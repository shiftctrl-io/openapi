# -*- coding: utf-8 -*-

import odoo
import json
import logging
import functools
import werkzeug.wrappers
import time
import datetime
import os
import pprint
from odoo.http import WebRequest, Response, serialize_exception, HttpRequest, Root, SessionExpiredException, AuthenticationError
from odoo.http import request, rpc_request, rpc_response, JsonRequest
from odoo.tools import pycompat, date_utils
from odoo.service.server import memory_info
from datetime import datetime, date

try:
    import psutil
except ImportError:
    psutil = None


_logger = logging.getLogger(__name__)
_request_stack = werkzeug.local.LocalStack()


class ApiJsonRequest(WebRequest):
    _request_type = "api"

    def __init__(self, *args):
        super(ApiJsonRequest, self).__init__(*args)

        self.jsonp_handler = None
        self.params = {}

        args = self.httprequest.args
        jsonp = args.get('jsonp')
        self.jsonp = jsonp
        request = None
        request_id = args.get('id')

        if jsonp and self.httprequest.method == 'POST':
            # jsonp 2 steps step1 POST: save call
            def handler():
                self.session['jsonp_request_%s' % (request_id,)] = self.httprequest.form['r']
                self.session.modified = True
                headers=[('Content-Type', 'text/plain; charset=utf-8')]
                r = werkzeug.wrappers.Response(request_id, headers=headers)
                return r
            self.jsonp_handler = handler
            return
        elif jsonp and args.get('r'):
            # jsonp method GET
            request = args.get('r')
        elif jsonp and request_id:
            # jsonp 2 steps step2 GET: run and return result
            request = self.session.pop('jsonp_request_%s' % (request_id,), '{}')
        else:
            # regular jsonrpc2
            request = self.httprequest.get_data().decode(self.httprequest.charset)

        # Read POST content or POST Form Data named "request"
        try:
            self.ApiJsonRequest = json.loads(request)
        except ValueError:
            msg = 'Invalid JSON data: %r' % (request,)
            _logger.info('%s: %s', self.httprequest.path, msg)
            raise werkzeug.exceptions.BadRequest(msg)

        self.params = dict(self.ApiJsonRequest.get("params", {}))
        self.context = self.params.pop('context', dict(self.session.context))


    def _json_response(self, result=None, error=None):

        response = {
            'jsonrpc': '2.0',
            }
        if error is not None:
            response['error'] = error
        if result is not None:
            response['status'] = result._status
        if result is not None:
            response['result'] = json.loads(result.data.decode())

        if self.jsonp:
            # If we use jsonp, that's mean we are called from another host
            # Some browser (IE and Safari) do no allow third party cookies
            # We need then to manage http sessions manually.
            response['session_id'] = self.session.sid
            mime = 'application/javascript'
            body = "%s(%s);" % (self.jsonp, json.dumps(response, default=date_utils.json_default))
        else:
            mime = 'application/json'
            body = json.dumps(response, default=date_utils.json_default)

        return Response(
            body, status=error and error.pop('http_status', 200) or 200,
            headers=[('Content-Type', mime), ('Content-Length', len(body))]
        )


    def _handle_exception(self, exception):
        """Called within an except block to allow converting exceptions
           to arbitrary responses. Anything returned (except None) will
           be used as response."""
        try:
            return super(ApiJsonRequest, self)._handle_exception(exception)
        except Exception:
            if not isinstance(exception, (odoo.exceptions.Warning, SessionExpiredException,
                                          odoo.exceptions.except_orm, werkzeug.exceptions.NotFound)):
                _logger.exception("Exception during JSON request handling.")
            error = {
                    'code': 200,
                    'message': "Odoo Server Error",
                    'data': serialize_exception(exception)
            }
            if isinstance(exception, werkzeug.exceptions.NotFound):
                error['http_status'] = 404
                error['code'] = 404
                error['message'] = "404: Not Found"
            if isinstance(exception, AuthenticationError):
                error['code'] = 100
                error['message'] = "Odoo Session Invalid"
            if isinstance(exception, SessionExpiredException):
                error['code'] = 100
                error['message'] = "Odoo Session Expired"
            return self._json_response(error=error)


    def dispatch(self):
        if self.jsonp_handler:
            return self.jsonp_handler()
        try:
            rpc_request_flag = rpc_request.isEnabledFor(logging.DEBUG)
            rpc_response_flag = rpc_response.isEnabledFor(logging.DEBUG)
            if rpc_request_flag or rpc_response_flag:
                endpoint = self.endpoint.method.__name__
                model = self.params.get('model')
                method = self.params.get('method')
                args = self.params.get('args', [])

                start_time = time.time()
                start_memory = 0
                if psutil:
                    start_memory = memory_info(psutil.Process(os.getpid()))
                if rpc_request and rpc_response_flag:
                    rpc_request.debug('%s: %s %s, %s',
                        endpoint, model, method, pprint.pformat(args))

            result = self._call_function(**self.params)

            if rpc_request_flag or rpc_response_flag:
                end_time = time.time()
                end_memory = 0
                if psutil:
                    end_memory = memory_info(psutil.Process(os.getpid()))
                logline = '%s: %s %s: time:%.3fs mem: %sk -> %sk (diff: %sk)' % (
                    endpoint, model, method, end_time - start_time, start_memory / 1024, end_memory / 1024, (end_memory - start_memory)/1024)
                if rpc_response_flag:
                    rpc_response.debug('%s, %s', logline, pprint.pformat(result))
                else:
                    rpc_request.debug(logline)

            return self._json_response(result)
        except Exception as e:
            return self._handle_exception(e)


#Copy of http.route adding routing 'type':'api'
def api_route(route=None, **kw):

    routing = kw.copy()
    assert 'type' not in routing or routing['type'] in ("http", "json", "api")
    def decorator(f):
        if route:
            if isinstance(route, list):
                routes = route
            else:
                routes = [route]
            routing['routes'] = routes
        @functools.wraps(f)
        def response_wrap(*args, **kw):
            response = f(*args, **kw)
            if isinstance(response, Response) or f.routing_type in ("api", "json"):
                return response

            if isinstance(response, (bytes, pycompat.text_type)):
                return Response(response)

            if isinstance(response, werkzeug.exceptions.HTTPException):
                response = response.get_response(request.httprequest.environ)
            if isinstance(response, werkzeug.wrappers.BaseResponse):
                response = Response.force_type(response)
                response.set_default()
                return response

            _logger.warn("<function %s.%s> returns an invalid response type for an http request" % (f.__module__, f.__name__))
            return response
        response_wrap.routing = routing
        response_wrap.original_func = f
        return response_wrap
    return decorator




def api_get_request(self, httprequest):
    # deduce type of request

    if httprequest.headers.get('Type') and httprequest.headers.get('Type') in ('api'):
        return ApiJsonRequest(httprequest)

    if httprequest.args.get('jsonp'):
        return JsonRequest(httprequest)

    if httprequest.mimetype in ("application/json", "application/json-rpc"):
        return JsonRequest(httprequest)

    else:
        return HttpRequest(httprequest)


Root.get_request = api_get_request
