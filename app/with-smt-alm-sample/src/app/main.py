#!/usr/bin/python3

# -*- coding:utf-8 -*-

import io
import sys
import os
import logging
import json
from datetime import datetime, timezone, timedelta
import urllib.parse
import requests
import boto3
from boto3.session import Session

import elasticsearch
from requests_aws4auth import AWS4Auth

import multiprocessing
import multiprocessing.pool
from multiprocessing import Pool

import random
from time import sleep

ES_INDICES = "user"

# logger設定
logger = logging.getLogger("app")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter(fmt="%(asctime)s:%(levelname)s:[%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

#'''
# stdout出力
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
stream_handler.setLevel(logging.DEBUG)
logger.addHandler(stream_handler)
#'''

'''
# ログファイル出力
fh = logging.FileHandler('/var/log/app/app.log', encoding='utf-8')
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
logger.addHandler(fh)
logger.propagate = False
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
'''

#### 処理時間計測用
from functools import wraps
import time
def stop_watch(func) :
    @wraps(func)
    def wrapper(*args, **kargs) :
        start = time.time()
        result = func(*args,**kargs)
        elapsed_time =  time.time() - start
        elapsed_time_fmt = "%.6f" % elapsed_time
#        print(f"func {func.__name__} elapsed {elapsed_time_fmt} second")
        logger.debug(f"{func.__name__} elapsed {elapsed_time} second")
        return result
    return wrapper

#### マルチプロセス関連
def argwrapper(args):
    '''
    Pool.map用ラッパー関数
    '''
    return args[0](*args[1:])

class NoDaemonProcess(multiprocessing.Process):
    # Poolをdaemonで起動しないようにするため
    def _get_daemon(self):
        return False
    def _set_daemon(self, value):
        pass
    daemon = property(_get_daemon, _set_daemon)

class MyPool(multiprocessing.pool.Pool):
    Process = NoDaemonProcess

#### ユーティリティ
def chunked(iterable, n):
    '''
    配列を任意のサイズで分割した配列を返却する
    '''
    return [iterable[x:x + n] for x in range(0, len(iterable), n)]

def post_json(**kwargs):
    post_data = kwargs.get('post_data')
    url = kwargs.get('url')
    res_json = {}
    headers = {'accept': 'application/json'}

    if post_data is not None:
        headers['Content-Type'] = 'application/json'

    try:
        data = json.dumps(post_data)
        response = requests.post(url, data=data, headers=headers, timeout=600)
        response.encoding = response.apparent_encoding
        status_code = response.status_code
        if (status_code == 200):
            res_json = response.json()
        else:
            logger.error("Requests error {}:{}".format(url, response.text))
    except Exception as e:
        logger.exception("Requests error {}:{}".format(url, e))
        raise e

    return res_json

@stop_watch
def do_start(param):
    return []


def null_response(start_response):
    start_response('200 OK', [('Content-type', 'text/json; charset=utf-8')])
    return [json.dumps({"hoge":"Hello World!"}, ensure_ascii=False).encode("utf-8")]

def do_process(env, start_response):

    start_time = time.time()

    if "REQUEST_URI" not in env:
        return null_response(start_response)
    if env["REQUEST_URI"] == "/":
        return null_response(start_response)

    if "QUERY_STRING" not in env:
        return null_response(start_response)
    if env["QUERY_STRING"] == "":
        return null_response(start_response)

    logger.info("env={}".format(env))

    alb_endpoint = env.get("ALB_ENDPOINT", None)
    es_endpoint = env.get("ES_ENDPOINT", None)

    query = urllib.parse.parse_qs(env.get("QUERY_STRING"))

    try:
        request_body_size = int(env.get('CONTENT_LENGTH', 0))
    except (ValueError):
        request_body_size = 0

#    request_body = env['wsgi.input'].read(request_body_size)
#    req_json = json.loads(request_body)
#    logger.debug("req_json={}".format(req_json))

    result_list = do_start(p_param)

    elapsed_time =  time.time() - start_time

    response = {
        "result": result_list,
        "send_user_cnt": send_user_cnt,
        "elapsed": elapsed_time,
    }
    start_response('200 OK', [('Content-type', 'text/json; charset=utf-8')])
    return [json.dumps(response, ensure_ascii=False).encode("utf-8")]

def application(env, start_response):
    try:
        return do_process(env, start_response)
    except Exception as e:
        logger.exception('Error: %s', e)

def dummy_start_response(*args):
    pass

if __name__ == '__main__':
    env = {
        "ALB_ENDPOINT": "",
        "ES_ENDPOINT": "",
    }
    response = application(env, dummy_start_response)
    logger.debug(response)
