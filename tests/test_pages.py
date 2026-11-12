"""
Local HTTP tests for the lazy page-batch interfaces
(:func:`mwapi.Session.pages` and :func:`mwapi.AsyncSession.pages`).

A scripted ``http.server`` serves pre-recorded API response documents in
order and logs every request it receives, so tests can assert on both
the merged page batches and the exact continuation requests that were
(or were not) sent.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests

import mwapi
from mwapi.errors import APIError


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        responses = []
        requests_log = []

        def _serve(self, params):
            type(self).requests_log.append(params)
            if not type(self).responses:
                doc = {'error': 'unexpected request'}
                status = 500
            else:
                doc = type(self).responses.pop(0)
                status = 200
            body = json.dumps(doc).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            params = {k: v[0] for k, v in
                      parse_qs(urlparse(self.path).query,
                               keep_blank_values=True).items()}
            self._serve(params)

        def do_POST(self):
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            params = {k: v[0] for k, v in
                      parse_qs(body, keep_blank_values=True).items()}
            self._serve(params)

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def api():
    handler = make_handler()
    httpd = HTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        host='http://127.0.0.1:{0}'.format(httpd.server_address[1]),
        handler=handler)
    httpd.shutdown()
    thread.join()


FV2_RESPONSES = [
    # request 0: a complete first batch, but the query continues
    {
        'batchcomplete': True,
        'continue': {'rccontinue': '20240101|0', 'continue': '||'},
        'query': {'pages': [
            {'pageid': 1, 'title': 'Alpha', 'revisions': [{'revid': 10}]},
        ]},
    },
    # request 1: second batch starts, asks for continuation
    {
        'continue': {'rccontinue': '20240101|1', 'continue': '||'},
        'query': {'pages': [
            {'pageid': 2, 'title': 'Beta', 'ns': 0,
             'revisions': [{'revid': 20}], 'info': {'x': 1, 'y': 1}},
            {'pageid': 3, 'title': 'Gamma', 'ns': 0},
        ]},
    },
    # request 2: more data for page 2, a missing page, still continuing
    {
        'continue': {'rccontinue': '20240102|2', 'continue': '||'},
        'query': {'pages': [
            {'pageid': 2, 'revisions': [{'revid': 21}],
             'info': {'y': 2, 'z': 3}},
            {'title': 'MissingPage', 'missing': True},
        ]},
    },
    # request 3: batchcomplete closes the second batch
    {
        'batchcomplete': True,
        'query': {'pages': [
            {'pageid': 3, 'title': 'Gamma', 'ns': 4,
             'categories': [{'title': 'Cat:X'}]},
        ]},
    },
]

FV2_EXPECTED_BATCHES = [
    [
        {'pageid': 1, 'title': 'Alpha', 'revisions': [{'revid': 10}]},
    ],
    [
        {'pageid': 2, 'title': 'Beta', 'ns': 0,
         'revisions': [{'revid': 20}, {'revid': 21}],
         'info': {'x': 1, 'y': 2, 'z': 3}},
        {'pageid': 3, 'title': 'Gamma', 'ns': 4,
         'categories': [{'title': 'Cat:X'}]},
        {'title': 'MissingPage', 'missing': True},
    ],
]

ERROR_RESPONSE = {
    'error': {'code': 'internal_api_error', 'info': 'boom',
              '*': 'see https://example.org/api'}
}


def sync_session(api, responses, formatversion=2):
    api.handler.responses = list(responses)
    # don't let proxy-related environment variables interfere with
    # requests to the local test server
    http_session = requests.Session()
    http_session.trust_env = False
    return mwapi.Session(api.host, user_agent='mwapi-test',
                         formatversion=formatversion, session=http_session)


def test_fv2_batches_merge_and_continue(api):
    session = sync_session(api, FV2_RESPONSES)
    batches = list(session.pages(action='query', list='recentchanges'))
    assert batches == FV2_EXPECTED_BATCHES

    log = api.handler.requests_log
    assert len(log) == 4
    # base params are preserved on every request
    for request in log:
        assert request['action'] == 'query'
        assert request['list'] == 'recentchanges'
        assert request['formatversion'] == '2'
    # only the latest continue fields are sent each round
    assert log[0]['continue'] == ''
    assert 'rccontinue' not in log[0]
    assert log[1]['rccontinue'] == '20240101|0'
    assert log[2]['rccontinue'] == '20240101|1'
    assert log[3]['rccontinue'] == '20240102|2'


def test_fv1_pages_dict_output(api):
    responses = [
        {'continue': {'rccontinue': 'x', 'continue': '||'},
         'query': {'pages': {
             '5': {'pageid': 5, 'title': 'Five'},
             '-1': {'title': 'Nope', 'missing': ''}}}},
        {'batchcomplete': True,
         'query': {'pages': {
             '5': {'pageid': 5, 'ns': 0},
             '-1': {'title': 'Nope', 'ns': 1}}}},
    ]
    session = sync_session(api, responses, formatversion=1)
    batches = list(session.pages(action='query'))
    assert batches == [[
        {'pageid': 5, 'title': 'Five', 'ns': 0},
        {'title': 'Nope', 'missing': '', 'ns': 1},
    ]]


def test_empty_batch(api):
    session = sync_session(api, [{'batchcomplete': True}])
    assert list(session.pages(action='query')) == [[]]

    session = sync_session(
        api, [{'batchcomplete': True, 'query': {'pages': []}}])
    assert list(session.pages(action='query')) == [[]]


def test_query_continue_attribute(api):
    session = sync_session(
        api, [{'batchcomplete': True, 'query': {'pages': []}}])
    query_continue = {'rccontinue': 'abc|1', 'continue': '||'}
    batches = list(session.pages(action='query',
                                 query_continue=query_continue))
    assert batches == [[]]
    request = api.handler.requests_log[0]
    assert request['rccontinue'] == 'abc|1'
    assert request['continue'] == '||'
    # the caller's dict is not modified
    assert query_continue == {'rccontinue': 'abc|1', 'continue': '||'}


def test_post_method(api):
    session = sync_session(api, [
        {'batchcomplete': True,
         'query': {'pages': [{'pageid': 7, 'title': 'Post'}]}},
    ])
    batches = list(session.pages(method='POST', action='query'))
    assert batches == [[{'pageid': 7, 'title': 'Post'}]]
    assert api.handler.requests_log[0]['action'] == 'query'


def test_stop_consuming_stops_requests(api):
    session = sync_session(api, FV2_RESPONSES)
    generator = session.pages(action='query')
    assert next(generator) == FV2_EXPECTED_BATCHES[0]
    generator.close()
    # only the requests needed for the first batch were made
    assert len(api.handler.requests_log) == 1


def test_sync_error(api):
    session = sync_session(api, [ERROR_RESPONSE])
    with pytest.raises(APIError) as exc_info:
        list(session.pages(action='query'))
    assert exc_info.value.code == 'internal_api_error'


async def _collect(async_session, **kwargs):
    return [batch async for batch in async_session.pages(**kwargs)]


async def test_async_fv2_matches_sync(api):
    session = sync_session(api, FV2_RESPONSES)
    sync_batches = list(session.pages(action='query', list='recentchanges'))
    sync_log = list(api.handler.requests_log)

    api.handler.responses = list(FV2_RESPONSES)
    api.handler.requests_log.clear()
    async_session = mwapi.AsyncSession(api.host, user_agent='mwapi-test',
                                       formatversion=2)
    try:
        async_batches = await _collect(async_session, action='query',
                                       list='recentchanges')
    finally:
        await async_session.session.close()

    assert async_batches == sync_batches == FV2_EXPECTED_BATCHES
    assert api.handler.requests_log == sync_log


async def test_async_empty_batch(api):
    api.handler.responses = [{'batchcomplete': True}]
    async_session = mwapi.AsyncSession(api.host, user_agent='mwapi-test',
                                       formatversion=2)
    try:
        assert await _collect(async_session, action='query') == [[]]
    finally:
        await async_session.session.close()


async def test_async_query_continue_attribute(api):
    api.handler.responses = [
        {'batchcomplete': True, 'query': {'pages': []}}]
    async_session = mwapi.AsyncSession(api.host, user_agent='mwapi-test',
                                       formatversion=2)
    try:
        query_continue = {'rccontinue': 'abc|1', 'continue': '||'}
        batches = await _collect(async_session, action='query',
                                 query_continue=query_continue)
    finally:
        await async_session.session.close()
    assert batches == [[]]
    request = api.handler.requests_log[0]
    assert request['rccontinue'] == 'abc|1'
    assert query_continue == {'rccontinue': 'abc|1', 'continue': '||'}


async def test_async_stop_consuming_stops_requests(api):
    api.handler.responses = list(FV2_RESPONSES)
    async_session = mwapi.AsyncSession(api.host, user_agent='mwapi-test',
                                       formatversion=2)
    try:
        generator = async_session.pages(action='query')
        batch = await generator.__anext__()
        assert batch == FV2_EXPECTED_BATCHES[0]
        await generator.aclose()
        assert len(api.handler.requests_log) == 1
    finally:
        await async_session.session.close()


async def test_async_error_matches_sync(api):
    session = sync_session(api, [ERROR_RESPONSE])
    with pytest.raises(APIError) as sync_exc_info:
        list(session.pages(action='query'))

    api.handler.responses = [ERROR_RESPONSE]
    api.handler.requests_log.clear()
    async_session = mwapi.AsyncSession(api.host, user_agent='mwapi-test',
                                       formatversion=2)
    try:
        with pytest.raises(APIError) as async_exc_info:
            await _collect(async_session, action='query')
    finally:
        await async_session.session.close()

    assert type(async_exc_info.value) is type(sync_exc_info.value)
    assert async_exc_info.value.code == sync_exc_info.value.code
    assert async_exc_info.value.info == sync_exc_info.value.info
