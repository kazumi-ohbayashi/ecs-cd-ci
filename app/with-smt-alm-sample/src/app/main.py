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

def geohash_grid(es, precision, filter_location):
    '''
    filter_locationで指定された矩形範囲について、
    Elasticsearchのgeohash_gridで定義されたprecisionでグリッド分割し、
    各グリッドのグリッド範囲（左上・右下の緯度経度）とユーザー数を取得する
    precision:
      3: 156.5km x 156km
      4: 39.1km x 19.5km
      5: 4.9km x 4.9km
      6: 1.2km x 609.4m
    '''

    tl = filter_location["top_left"]
    br = filter_location["bottom_right"]

    if tl["lat"] == br["lat"] and tl["lon"] == br["lon"]:
        # top_leftとbottom_rightが同じ場合、エラーとなってしまうので、
        # 0.001度の矩形を強制的に作成した上で検索実施
        tl = {
            "lat": tl["lat"] + 0.0005,
            "lon": tl["lon"] - 0.0005
        }
        br = {
            "lat": br["lat"] - 0.0005,
            "lon": br["lon"] + 0.0005
        }

    #### grid取得
    body = {
      "size": 0,
      "aggs" : {
        "grid" : {
          "filter" : {
            "geo_bounding_box" : {
              "location" : {
                "top_left" : {
                  "lat": tl["lat"],
                  "lon": tl["lon"]
                },
                "bottom_right" : {
                  "lat": br["lat"],
                  "lon": br["lon"]
                }
              }
            }
          },
          "aggs":{
            "grid_list":{
              "geohash_grid" : {
                "field": "location",
                "precision": precision
              },
              "aggs": {
                "viewport": {
                  "geo_bounds": {
                    "field": "location"
                  }
                }
              }
            }
          }
        }
      }
    }

    res = es.search(index=ES_INDICES,
        body=body
    )

    grid_list = []
    for r in res["aggregations"]["grid"]["grid_list"]["buckets"]:
        r = {
            "doc_count": r["doc_count"],
            "viewport": r["viewport"]["bounds"],
        }
        grid_list.append(r)

    return grid_list

@stop_watch
def devide_by_grid(es, precision, grid_list):
    '''
    grid_listの各グリッドをprecision単位でグリッド分割し、
    分割したグリッドの全リストを取得する
    '''
    zoomin_grid_list = []
    for grid in grid_list:
        tl = grid["viewport"]["top_left"]
        br = grid["viewport"]["bottom_right"]
        filter_location = {
            "top_left": {
                "lat": tl["lat"],
                "lon": tl["lon"]
            },
            "bottom_right": {
                "lat": br["lat"],
                "lon": br["lon"]
            }
        }

        g_list = geohash_grid(es, precision, filter_location)
        zoomin_grid_list.extend(g_list)

    return zoomin_grid_list

@stop_watch
def do_start(param):
    '''
        p_param = {
         "alb_endpoint": alb_endpoint,
          "es_endpoint": es_endpoint,
          "es_precision": es_precision,
          "grid_num_per_node": grid_num_per_node,
          "sns_num_per_node": sns_num_per_node,
        }
    '''
#    awsauth = AWS4Auth(ACCESS_KEY, SECRET_KEY, REGION, 'es')
    es = elasticsearch.Elasticsearch(
        hosts=[{'host': param["es_endpoint"], 'port': 443}],
#        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=elasticsearch.connection.RequestsHttpConnection
    )

    ## ESよりグリッド分割単位:4(39.1km x 19.5km)で、グリッド情報を取得。
    ## いきなりグリッド分割単位:5(4.9km x 4.9km)で分割した場合、
    ## 1回のgeohash_gridでESから返却されるドキュメント数は最大で10000件のため、
    ## 全グリッド情報を取得することができない。
    ## したがって、分割単位:4で分割した結果に対して、再度分割を行う必要がある。
    # 日本全域(左上-右下)
    jp_filter_location = {
        "top_left": {
            "lat": 50,
            "lon": 120
        },
        "bottom_right": {
            "lat": 20,
            "lon": 155
        }
    }
    grid4_list = geohash_grid(es, 4, jp_filter_location)
    logger.debug("grid4_len={}".format(len(grid4_list)))

    ## グリッド分割単位でグリッドを詳細分割
    grid_list = devide_by_grid(es, param["es_precision"], grid4_list)
    user_cnt = 0
    for g in grid_list:
        # {'doc_count': 1442, 'viewport': {'top_left': {'lat': 35.72753248270601, 'lon': 139.70221356488764}, 'bottom_right': {'lat': 35.68362208083272, 'lon': 139.74609333090484}}}
        user_cnt = user_cnt + g["doc_count"]
    logger.debug("all grid{}_len={}, user_cnt={}".format(param["es_precision"], len(grid_list), user_cnt))

    # グリッドを分割
    sub_grid_list = chunked(grid_list, param["grid_num_per_node"])

    return []


def null_response(start_response):
    start_response('200 OK', [('Content-type', 'text/json; charset=utf-8')])
    return [json.dumps({}, ensure_ascii=False).encode("utf-8")]

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

    # ESグリッド分割レベル
    if "es_precision" in query:
        es_precision = int(query["es_precision"][0])
        if es_precision != 5 and es_precision != 6:
            raise Exception("es_precision error")
    else:
        es_precision = 5

    # グリッド処理分割数
    if "grid_num_per_node" in query:
        grid_num_per_node = int(query["grid_num_per_node"][0])
        if grid_num_per_node < 100:
            raise Exception("grid_num_per_node error")
    else:
        grid_num_per_node = 1000

    try:
        request_body_size = int(env.get('CONTENT_LENGTH', 0))
    except (ValueError):
        request_body_size = 0

#    request_body = env['wsgi.input'].read(request_body_size)
#    req_json = json.loads(request_body)
#    logger.debug("req_json={}".format(req_json))
    p_param = {
        "alb_endpoint": alb_endpoint,
        "es_endpoint": es_endpoint,
        "es_precision": es_precision,
        "grid_num_per_node": grid_num_per_node,
    }

    result_list = do_start(p_param)

    send_user_cnt = 0
    for r in result_list:
        send_user_cnt = send_user_cnt + int(r["send_user_len"])


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
