#!/usr/bin/python
# -*- coding: utf-8 -*-
# author:   Jan Hybs
from datetime import datetime
import os
import time

from pymongo import MongoClient
import sys
from flowrunner.utils import rget, rpluck
from flowrunner.utils.json_preprocessor import JsonPreprocessor
from flowrunner.utils.logger import Logger
from flowrunner.utils.strings import read_json, to_json, secure_values
from flowrunner.utils import io, lists, math
from flowrunner.utils.timer import Timer

logger = Logger(__name__)
timer = Timer()

_ID = '_id'
_PUSH = '$push'
_EACH = '$each'
_SORT = '$sort'
_SLICE = '$slice'
_MATCH = '$match'
_GROUP = '$group'
_SET = '$set'
_INC = '$inc'

PATH = 'path'
MEAS = 'meas'

ID_IS = lambda x: { _ID: x }


class Collections(object):
    def __init__(self, db):
        """
        :type db: pymongo.database.Database
        """
        self._collections = set(db.collection_names())
        self.environment = db.get_collection('environment')
        self.calibration = db.get_collection('calibration')
        self.context = db.get_collection('context')
        self.metrics = db.get_collection('metrics')
        self.pts = db.get_collection('pts')

    def exists(self, collection):
        return collection in self._collections

    def __repr__(self):
        mandatory = ['environment', 'calibration', 'context', 'pts']
        return "<Collections: {status}>".format(status=', '.join(
            ["{name}={status}".format(name=name, status=name in self._collections)
             for name in set(mandatory).union(self._collections)]))


class MongoDB(object):
    def __init__(self, host='localhost', port=27017, database='flow'):
        self.client = MongoClient(host, port)
        self.flowdb = self.client.get_database(database)
        self.collections = Collections(self.flowdb)

        self.open()


    def open(self):
        self._create_collection_environment()
        self._create_collection_calibration()
        self._create_collection_context()
        self._create_collection_metrics()
        self._create_collection_pts()

    def _create_collection_environment(self, name='environment'):
        self._generic_create_collection(name, [
            # cpu indices
            'arch.cpu.x64',
            'arch.cpu.frequency',
            'arch.cpu.avail',
            # memory indices
            'arch.memory.avail'
        ])

    def _create_collection_calibration(self, name='calibration'):
        self._generic_create_collection(name, ['cpu', 'memory'])

    def _create_collection_context(self, name='context'):
        self._generic_create_collection(name, ['nproc', 'task_size', 'problem'])

    def _create_collection_metrics(self, name='metrics'):
        self._generic_create_collection(name, ['duration'])

    def _create_collection_pts(self, name='pts'):
        self._generic_create_collection(name, [
            'path',
            'context.cal.cpu', 'context.version', 'context.nproc', 'context.problem'
            'metrics.duration'
            ])

    def _generic_create_collection(self, name, indices):
        if self.collections.exists(name):
            logger.debug("collection with name '{name}' exists".format(name=name))
            return

        logger.debug("creating collection '{name}'...".format(name=name))
        self.flowdb.create_collection(name)

        with logger:
            logger.debug("creating indices for collection '{name}'".format(name=name))
            self._create_indices(getattr(self.collections, name), indices)

    @staticmethod
    def _create_indices(collection, indices):
        """
        :type collection:  pymongo.collection.Collection
        :type indices: list
        """
        with logger:
            for index in indices:
                logger.debug("creating index '{index}'".format(index=index))
                collection.create_index(index)

    @staticmethod
    def _extract_process_context(json_data):
        """
        :type json_data: dict
        :rtype : dict
        """
        fields = dict(flags='program-flags', branch='program-branch', problem='problem',
                      path='path', task_size='task-size', resolution='timer-resolution',
                      start='run-started-at', nproc='nproc', version='program-revision')

        context = JsonPreprocessor.extract_props(json_data, fields)
        context = JsonPreprocessor.convert_fields(
            context, lambda x: datetime.strptime(x, '%m/%d/%y %H:%M:%S'), ['start'])
        return context

    @staticmethod
    def _extract_process_metrics(json_data):
        """
        :type json_data: dict
        :rtype : dict
        """
        fields = dict(exit='exit', duration='duration')

        context = JsonPreprocessor.extract_props(json_data, fields)
        context = JsonPreprocessor.convert_fields(context, int, ['exit'])
        context = JsonPreprocessor.convert_fields(context, float, ['duration'])
        return context

    @staticmethod
    def _extract_event_context(json_data):
        """
        :type json_data: dict
        :rtype : dict
        """
        fields = dict(path='file-path', function='function', tag='tag', line='file-line')

        context = JsonPreprocessor.extract_props(json_data, fields)
        return context

    @staticmethod
    def _extract_event_metrics(json_data):
        """
        :type json_data: dict
        :rtype : dict
        """
        fields = dict(call='call-count', call_max='call-count-max',
                      call_min='call-count-min', call_sum='call-count-sum',
                      duration='cumul-time',

                      time='cumul-time', time_max='cumul-time-max',
                      time_min='cumul-time-min', time_sum='cumul-time-sum')

        context = JsonPreprocessor.extract_props(json_data, fields)
        context = JsonPreprocessor.convert_fields(
            context, lambda x: datetime.strptime(x, '%m/%d/%y %H:%M:%S'), ['start'])
        return context

    def _extract_environment(self, filename):
        """
        Method will process given json filename. Method will also remove entries
            having no information (like missing binaries)
        :param filename:
        :return:
        """
        json_data = read_json(filename)
        JsonPreprocessor.filter(json_data['bins'], 'missing', lambda x: bool(x))
        JsonPreprocessor.delete_props(json_data, ['bins'])
        return json_data

    def insert_environment(self, json_data):
        """
         Method will insert given object to collection environment
        :rtype : pymongo.results.InsertOneResult
        :type filename: str
        """
        return self.collections.environment.insert_one(json_data)

    def _extract_calibration(self, filename):
        """
        Method will process given json filename.
            Processing json is done by analyzing measured values in json file. Only 3 values
             (cpu, memory, combination) will be stored in db
        :rtype : dict
        :type filename: str
        """
        json_data = read_json(filename)
        return {
            'cpu': math.avg(rpluck(json_data, 'tests.for-loop.effectiveness')),
            'memory': math.avg(rpluck(json_data, 'tests.string-concat.effectiveness')),
            'complex': math.avg(rpluck(json_data, 'tests.matrix-solve.effectiveness'))
        }


    def insert_calibration(self, filename):
        """
        Method will process given json filename and will insert it into
            database, collection environment attaching it to existing environment document.
            Processing json is done by analyzing measured values in json file. Only 3 values
             (cpu, memory, combination) will be stored in db
        :rtype : pymongo.results.InsertOneResult
        :type filename: str
        """
        return self.collections.calibration.insert_one(self._extract_calibration(filename))

    def attach_calibration(self, filename, env_id):
        """
        Method will process given json filename and will insert it into
            database, collection calibration. Processing json is done by analyzing measured
            values in json file. Only 3 values (cpu, memory, combination) will be stored in db
        :rtype : pymongo.results.UpdateResult
        :type filename: str
        """
        return self.collections.environment.update_one(
            ID_IS(env_id), {
                _SET: { 'calibration': self._extract_calibration(filename) }
            })

    def remove_all(self):
        logger.debug("Dropping database 'flow'")
        return self.client.drop_database('flow')

    def insert_context(self, context):
        return self.collections.context.insert_one(context)

    def insert_metrics(self, metrics):
        return self.collections.metrics.insert_one(metrics)

    def insert_process(self, dirname, env):
        profilers = io.browse(dirname)
        profilers = lists.filter(profilers, lambda x: str(x).lower().find('info-') != -1)

        for profiler in profilers:
            json_data = read_json(profiler)
            context = self._extract_process_context(json_data)
            metrics = self._extract_process_metrics(json_data)

            context.update(dict(env=env, process=True))

            # metrics_id = self.insert_metrics(metrics.copy()).inserted_id
            # context_id = self.insert_context(context.copy()).inserted_id

            self._insert_pts_simple(',', metrics, context)

            child_context = context.copy()
            child_context.update(dict(process=False))
            if 'children' in json_data:
                whole_program = json_data['children'][0]
                self.insert_time_frame(whole_program, [], context=child_context)

        return context, metrics

    def insert_time_frame(self, node, parents, **kwargs):
        """
        :type node: dict
        :type parents: list
        """
        path = parents[:]
        path = path + [node.get('tag')]
        path_repr = to_path(secure_values(path))

        meas = node.copy()

        context = self._extract_event_context(meas)
        metrics = self._extract_event_metrics(meas)
        new_context = kwargs.get('context', {}).copy()
        new_context.update(context)
        context = new_context

        self._insert_pts_simple(path_repr, metrics, context)
        # metrics_id = self.insert_metrics(metrics.copy()).inserted_id
        #
        # context_document = self.collections.context.find_one(context)
        # if context_document:
        #     context_id = context_document['_id']
        # else:
        #     context_id = self.insert_context(context.copy()).inserted_id
        #
        # self._insert_pts(path_repr, metrics, context)

        for child in node.get('children', []):
            self.insert_time_frame(child, path, context=context)

    def _insert_pts_simple(self, path_repr, metrics, context):
        return self.collections.pts.insert_one({
            'path': path_repr,
            'context': context,
            'metrics': metrics,
        })

    def _insert_pts(self, path_repr, metrics_id, context_id):
        if not self._pts_node_exists(path_repr):
            self.collections.pts.insert_one(
                {
                    _ID: path_repr,
                    MEAS: [
                        {
                            'context': context_id,
                            'metrics': metrics_id
                        }
                    ]
                }
            )
        else:
            self.collections.pts.update_one(
                ID_IS(path_repr),
                {
                    _PUSH: {
                        MEAS: {
                            'context': context_id,
                            'metrics': metrics_id
                        }
                    }
                }
            )

    def _pts_node_exists(self, path):
        return bool(self.collections.pts.find({ _ID: path }).count())

    def close(self):
        self.client.close()


def to_path(values):
    return ',' + ','.join(values) + (',' if len(values) else '')


def fetch_all(cursor):
    return [item for item in cursor]


    # m = MongoDB()
    # m.remove_all()
    # m.client.close()
    #
    # m = MongoDB()
    # time.sleep(.01)
    # print m.insert_environment(r'./tests/test-02/environment.json')
    # print m.attach_calibration(r'./tests/test-02/performance.json')
    # for i in range(10):
    # with timer.measured('index {i}'.format(i=i)):
    # m.insert_process(r'./tests/test-02')
    #
    # ctxs = set()
    # for item in m.collections.pts.aggregate([
    # { _MATCH: { } },
    # { _GROUP: { _ID: '$path', 'contexts': { _PUSH: "$meas.context" } } }]):
    #     ctxs = ctxs.union(set(item['contexts'][0]))
    # print ctxs