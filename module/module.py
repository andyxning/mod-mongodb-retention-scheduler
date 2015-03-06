# -*- coding: utf-8 -*-
# 
# Copyright @ 2015 OPS, Qunar Inc. (qunar.com)
# Author: ning.xie <andy.xning@qunar.com>
# 

import os
import time
import signal
import base64
import traceback

from multiprocessing import Process

try:
    import cPickle as pickle
except ImportError:
    import pickle as pickle

try:
    from pymongo import MongoReplicaSetClient, MongoClient
    from pymongo.errors import (ConnectionFailure, InvalidURI,
                                ConfigurationError)
except ImportError:
    raise Exception('Python binding for MongoDB has not been installed. '
                    'Please install "pymongo" first')
    

from shinken.basemodule import BaseModule
from shinken.log import logger
from shinken.util import to_bool


properties = {
              'daemons': ['scheduler'],
              'type': 'mongodb-retention-scheduler',
              'external': False
              }


# called by the plugin manager to get a mongodb_retention_scheduler instance
def get_instance(mod_conf):
    logger.info('[Mongodb-Retention-Scheduler] Get a Scheduler module %s' 
                % mod_conf.get_name())
    instance = MongodbRetentionScheduler(mod_conf)
    return instance


# Main class
class MongodbRetentionScheduler(BaseModule):
    
    def __init__(self, mod_conf):
        BaseModule.__init__(self, mod_conf)
        self._parse_conf(mod_conf)
        
        self.conn = None
        self.task = None
        self.update_count = 0
        
    def _parse_conf(self, mod_conf):
        self.high_availability = to_bool(getattr(mod_conf,
                                                 'high_availability', 'false'))
        if not self.high_availability:
            self.stand_alone = getattr(mod_conf, 'stand_alone', '')
            if not self.stand_alone:
                logger.error('[Mongodb-Retention-Scheduler] Mongodb is '
                             'configured with high availability be false but '
                             'stand_alone is not configured')
                raise Exception('[Mongodb-Retention-Scheduler] Configuration '
                                'Error')
        else:
            replica_set_str = getattr(mod_conf, 'replica_set', '')
            self._set_replica_set(replica_set_str)
        
        self.database = getattr(mod_conf,
                                'database', 'shinken_retention_scheduler')
        self.username = getattr(mod_conf,
                                'username', 'shinken_scheduler_retention')
        self.password = getattr(mod_conf,
                                'password', 'shinken-scheduler_retention')
        self.url_options = getattr(mod_conf, 'url_options', '')
        
        try:
            self.retention_multiplier = int(getattr(mod_conf,
                                                    'retention_multiplier'))
        except:
            self.retention_multiplier = 30
        
    def _set_replica_set(self, replica_set_str):
        raw_members = replica_set_str.split(',')
        members = []
        for member in raw_members:
            members.append(member.strip())
        self.replica_set = members        
        
    def _set_mongodb_url(self):
        scheme = 'mongodb://'
        db_and_options = '/%s?%s' % (self.database, self.url_options) 
        credential = ':'.join((self.username, '%s@' % self.password))
        if not self.high_availability:
            address = self.stand_alone
            mongodb_url = ''.join((scheme, credential, address, db_and_options))
        else:
            address = ','.join(self.replica_set)
            mongodb_url = ''.join((scheme, credential, address, db_and_options))
        self.mongodb_url = mongodb_url
        
    # Called by Scheduler to do init work
    def init(self):
        logger.info('[Mongodb-Retention-Scheduler] Initialization of '
                    'mongodb_retention_scheduler module')
        self._set_mongodb_url()
        logger.debug('[Mongodb-Retention-Scheduler] Mongodb connect url: %s' 
                     % self.mongodb_url)
    
    def _init(self):
        try:
            if not self.high_availability:
                self.conn = MongoClient(self.mongodb_url)
            else:
                self.conn = MongoReplicaSetClient(self.mongodb_url)
        except ConnectionFailure:
            logger.warn('[Mongodb-Retention-Scheduler] Can not make connection '
                        ' with MongoDB')
            raise
            
        except (InvalidURI, ConfigurationError):
            logger.warn('[Mongodb-Retention-Scheduler] Mongodb connect url '
                        'error')
            logger.warn('[Mongodb-Retention-Scheduler] Mongodb connect url: %s' 
                        % self.mongodb_url)
            raise 
        self._get_collections()
        
    def _get_collections(self):
        db = self.conn[self.database]
        self.host_retentions = db['host_retentions']
        self.service_retentions = db['service_retentions']
        
    def _do_stop(self):
        if self.conn:
            self.conn.close()
            self.conn = None
                
    # invoked by the Scheduler daemon
    # We must not do any thing that will last for a long time. It will delay 
    # other operation in the Scheduler daemon's main event loop.
    def hook_save_retention(self, daemon):
        
        self.update_count += 1
        if self.update_count == self.retention_multiplier:
            retention = daemon.get_retention_data()
            if self.task and self.task.is_alive():
                logger.warn('[Mongodb-Retention-Scheduler] Previous task has '
                            'not been accomplished but this should not happen. '
                            'We should stop it.')
                os.kill(self.task.pid, signal.SIGKILL)
            self.task = None
            # must be args=(retention,) not args=(retention)
            self.task = Process(target=self._hook_save_retention,
                                args=(retention,))
            self.task.daemon = True
            self.task.start()
            logger.info('[Mongodb-Retention-Scheduler] New update begins.')
            
            self.update_count = 0
        
    def _hook_save_retention(self, retention):
        self.set_proctitle(self.name)
        try:
            self._init()
        except Exception:
            logger.warn('[Mongodb-Retention-Scheduler] Update retention '
                        'ends error.')
            return
            
        self._update_retention(retention)
        self._do_stop()    
    
    # invoked by the Scheduler daemon
    def hook_load_retention(self, daemon):
        logger.info('[Mongodb-Retention-Scheduler] Load retention starts.')
        try:
            self._init()
        except Exception:
            logger.warn('[Mongodb-Retention-Scheduler] Load retention '
                        'ends error.')
            return
        
        hosts = {}
        services = {}
        restored_hosts = {}
        restored_services = {}
        try:
            host_cursor = self.host_retentions.find()
            service_cursor = self.service_retentions.find()
            for host in host_cursor:
                value = host.get('value')
                restored_hosts[host.get('_id')] = value 
            for service in service_cursor:
                value = service.get('value')
                restored_services[service.get('_id')] = value
            for host in daemon.hosts:
                key = 'HOST-%s' % host.host_name
                if key in restored_hosts:
                    restored_value = restored_hosts[key]
                    value = pickle.loads(base64.b64decode(restored_value))
                    hosts[host.host_name] = value
            for service in daemon.services:
                key = 'SERVICE-%s,%s' % (service.host.host_name,
                                         service.service_description)
                if key in restored_services:
                    restored_value = restored_services[key]
                    value = pickle.loads(base64.b64decode(restored_value))
                    services[(service.host.host_name,
                              service.service_description)] = value
            
            retention_data = {'hosts': hosts, 'services': services}  
            daemon.restore_retention_data(retention_data)   
            
            logger.info('[Mongodb-Retention-Scheduler] Retention load ends '
                        'successfully.')
        except Exception:
            logger.warn('[Mongodb-Retention-Scheduler] Retention load error.')
            logger.warn('[Mongodb-Retention-Scheduler] %s' 
                        % traceback.format_exc())
        finally:
            self._do_stop()

    def _update_retention(self, data):
        logger.info('[Mongodb-Retention-Scheduler] Update retention starts.')
        hosts = data['hosts']
        services = data['services']
        try:
            for host in hosts:
                _id = 'HOST-%s' % host
                host_retention = hosts[host]
                dumped_value = pickle.dumps(host_retention,
                                            protocol=pickle.HIGHEST_PROTOCOL)
                value = base64.b64encode(dumped_value)
                self.host_retentions.remove({'_id': _id})
                retention_data = {'_id': _id,
                                  'value': value,
                                  'timestamp': int(time.time())
                                  }
                self.host_retentions.insert(retention_data)
            
            for (host, service) in services:
                _id = 'SERVICE-%s,%s' % (host, service)
                service_retention = services[(host, service)]
                dumped_value = pickle.dumps(service_retention,
                                            protocol=pickle.HIGHEST_PROTOCOL)
                value = base64.b64encode(dumped_value)
                self.service_retentions.remove({'_id': _id})
                retention_data = {'_id': _id,
                                  'value': value,
                                  'timestamp': int(time.time())
                                  }
                self.service_retentions.insert(retention_data)
            logger.info('[Mongodb-Retention-Scheduler] Update Retention ends'
                        'successfully.')
        except Exception:
            logger.warn('[Mongodb-Retention-Scheduler] Update Retention ends '
                        'error.')
            logger.warn('[Mongodb-Retention-Scheduler] %s' 
                        % traceback.format_exc())