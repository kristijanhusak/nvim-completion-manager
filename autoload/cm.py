# -*- coding: utf-8 -*-

# For debugging
# NVIM_PYTHON_LOG_FILE=nvim.log NVIM_PYTHON_LOG_LEVEL=INFO nvim

import os
import sys
import re
import logging
import copy
import importlib
import threading
from threading import Thread, RLock
import urllib
import json
from neovim import attach, setup_logging
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)


class Handler:

    def __init__(self,nvim):
        self._nvim = nvim

        # { '{source_name}': {'startcol': , 'matches'}
        self._matches = {}
        self._sources = {}
        self._last_matches = []
        self._has_popped_up = False

        self._file_server = FileServer()
        self._file_server.start(self._nvim.eval('v:servername'))

    def cm_complete(self,srcs,name,ctx,startcol,matches,*args):

        self._sources = srcs

        try:

            # process the matches early to eliminate unnecessary complete function call
            result = self.process_matches(name,ctx,startcol,matches)

            if (not result) and (not self._matches.get(name,{}).get('last_matches',[])):
                # not popping up, ignore this request
                logger.info('Not popping up, not refreshing for cm_complete by %s, startcol %s, matches %s', name, startcol, matches)
                return

        finally:

            # storing matches

            if name not in self._matches:
                self._matches[name] = {}

            if len(matches)==0:
                del self._matches[name]
            else:
                self._matches[name]['startcol'] = startcol
                self._matches[name]['matches'] = matches

        # wait for cm_complete_timeout, reduce flashes
        if self._has_popped_up:
            self._refresh_completions(ctx)

    def cm_insert_enter(self):
        self._matches = {}

    def cm_complete_timeout(self,srcs,ctx,*args):
        if not self._has_popped_up:
            self._refresh_completions(ctx)
            self._has_popped_up = True

    # The completion core itself
    def cm_refresh(self,srcs,ctx,*args):

        # update file server
        self._file_server.set_current_context(ctx)
        ctx['file_url'] = self._file_server.get_file_url(ctx)

        self._sources = srcs
        self._has_popped_up = False

        # simple complete done
        if ctx['typed'] == '':
            self._matches = {}
        elif re.match(r'[^0-9a-zA-Z_]',ctx['typed'][-1]):
            self._matches = {}

        # do notify_sources_to_refresh
        refreshes_calls = []
        refreshes_channels = []
        for name in srcs:
            info = srcs[name]
            try:

                if not self._check_scope(ctx,info):
                    logger.info('source %s _check_scope failed for context <%s>, ignore it', name, ctx)
                    continue

                if (info['name'] in self._matches) and (info.get('refresh',0)==0):
                    # no need to refresh
                    continue

                if 'cm_refresh' in info:
                    refreshes_calls.append(name)
                for channel in info.get('channels',[]):
                    if 'id' in channel:
                        refreshes_channels.append(dict(name=name,id=channel['id'],context=ctx))
            except Exception as inst:
                logger.error('cm_refresh process exception: %s', inst)
                continue

        if not refreshes_calls and not refreshes_channels:
            logger.info('not notifying any channels, _refresh_completions now')
            self._refresh_completions(ctx)
            self._has_popped_up = True
        else:
            logger.info('cm#notify_sources_to_refresh [%s] [%s] [%s]', refreshes_calls, refreshes_channels, ctx)
            self._nvim.call('cm#notify_sources_to_refresh', refreshes_calls, refreshes_channels, ctx)

    # almost the same as `s:check_scope` in `autoload/cm.vim`
    def _check_scope(self,ctx,info):
        scopes = info.get('scopes',['*'])
        cur_scope = ctx.get('scope',ctx['filetype'])
        for scope in scopes:
            if scope=='*':
                return True
            if scope==cur_scope:
                return True
        return False

    def _refresh_completions(self,ctx):

        matches = []

        # sort by priority
        names = sorted(self._matches.keys(),key=lambda x: self._sources[x]['priority'], reverse=True)

        if len(names)==0:
            logger.info('_refresh_completions names: %s, startcol: %s, matches: %s', names, ctx['col'], matches)
            self._complete(ctx, ctx['col'], [])
            return

        startcol = ctx['col']
        base = ctx['typed'][startcol-1:]

        # basick processing per source
        for name in names:

            try:
                source_startcol = self._matches[name]['startcol']
                if source_startcol>ctx['col']:
                    self._matches[name]['last_matches'] = []
                    logger.error('ignoring invalid startcol: %s', self._matches[name])
                    continue

                source_matches = self._matches[name]['matches']
                source_matches = self.process_matches(name,ctx,source_startcol,source_matches)

                self._matches[name]['last_matches'] = source_matches

                if not source_matches:
                    continue

                # min non empty source_matches's source_startcol as startcol
                if source_startcol < startcol:
                    startcol = source_startcol

            except Exception as inst:
                logger.error('_refresh_completions process exception: %s', inst)
                continue

        # merge processing results of sources
        for name in names:

            try:
                source_startcol = self._matches[name]['startcol']
                if source_startcol>ctx['col']:
                    logger.error('ignoring invalid startcol: %s', self._matches[name])
                    continue
                source_matches = self._matches[name]['last_matches']
                prefix = ctx['typed'][startcol-1 : source_startcol-1]

                for e in source_matches:
                    e['word'] = prefix + e['word']
                    # if 'abbr' in e:
                    #     e['abbr'] = prefix + e['abbr']

                matches += source_matches

            except Exception as inst:
                logger.error('_refresh_completions process exception: %s', inst)
                continue

        logger.info('_refresh_completions names: %s, startcol: %s, matches: %s, source matches: %s', names, startcol, matches, self._matches)
        self._complete(ctx, startcol, matches)

    def process_matches(self,name,ctx,startcol,matches):

        # do some basic filtering and sorting
        result = []
        base = ctx['typed'][startcol-1:]

        for item in matches:

            e = {}
            if type(item)==type(''):
                e['word'] = item
            else:
                e = copy.deepcopy(item)

            if 'menu' not in e:
                if 'info' in e and e['info'] and len(e['info'])<70:
                    if self._sources[name].get('abbreviation',''):
                        e['menu'] = self._sources[name]['abbreviation'] + " :" + e['info']
                    else:
                        e['menu'] = e['info']
                else:
                    e['menu'] = self._sources[name].get('abbreviation','')

            # For now, simply do the same word filtering as vim's doing
            # TODO: enable custom config
            if base.lower() != e['word'][0:len(base)].lower():
                continue

            result.append(e)

        # for now, simply sort them by length
        # TODO: enable custom config
        result.sort(key=lambda e: len(e['word']))

        return result


    def _complete(self, ctx, startcol, matches):
        if len(matches)==0 and len(self._last_matches)==0:
            # no need to fire complete message
            return
        self._nvim.call('cm#core_complete', ctx, startcol, matches, self._matches, async=True)

    def cm_shutdown(self):
        self._file_server.shutdown(wait=False)

# Cached file content in memory, and use http protocol to serve files, this
# would be convinent for supporting language server protocol.
class FileServer(Thread):

    def __init__(self):
        self._rlock = RLock()
        self._current_context = None
        self._cache_context = None
        self._cache_src = ""
        Thread.__init__(self)

    def start(self,nvim_server_name):
        """
        Start the file server
        @type request: str
        """

        server = self

        class HttpHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    server.run_GET(self)
                except Exception as ex:
                    self.send_response(500)
                    self.send_header('Content-type','text/html')
                    self.end_headers()
                    message = str(ex)
                    self.wfile.write(bytes(message, "utf8"))

        # create another connection to avoid synchronization issue?
        self._nvim = attach('socket',path=nvim_server_name)

        # Server settings
        # 0 for random port
        server_address = ('127.0.0.1', 0)
        self._httpd = HTTPServer(server_address, HttpHandler)

        Thread.start(self)

    def run_GET(self,request):
        """
        Process get request. This method, with the `run_` prefix is running on
        the same thread as `self.run` method.
        @type request: BaseHTTPRequestHandler
        """

        params = {}
        for e in urllib.parse.parse_qsl(urllib.parse.urlparse(request.path).query):
            params[e[0]] = e[1]
        
        logger.info('thread %s processing %s', threading.get_ident(), params)

        context = json.loads(params['context'])
        src = self.get_src(context)
        if src is None:
            src = ""

        request.send_response(200)
        request.send_header('Content-type','text/html')
        request.end_headers()
        request.wfile.write(bytes(src, "utf8"))

    def run(self):
        logger.info('running server on port %s, thread %s', self._httpd.server_port, threading.get_ident())
        self._httpd.serve_forever()

    def get_src(self,context):

        with self._rlock:

            # If context does not match current context, check the neovim current
            # context, if does not match neither, return None
            if self._context_changed(self._current_context,context):
                self._current_context = self._nvim.eval('cm#context()')
            if self._context_changed(self._current_context,context):
                logger.info('get_src returning None for oudated context: %s', context)
                return None

            # update cache when necessary
            if self._context_changed(self._current_context, self._cache_context):
                logger.info('get_src updating cache for context %s', context)
                self._cache_context = self._current_context
                self._cache_src = "\n".join(self._nvim.current.buffer[:])

            return self._cache_src

    # same as cm#context_changed
    def _context_changed(self,ctx1,ctx2):
        return ctx1 is None or ctx2 is None or ctx1['changedtick']!=ctx2['changedtick'] or ctx1['curpos']!=ctx2['curpos']

    def set_current_context(self,context):
        """
        This method is running on main thread as cm core
        """
        with self._rlock:
            self._current_context = context

    def get_file_url(self,context):
        # changedtick and curpos is enough for outdating check
        stripped = dict(changedtick=context['changedtick'],curpos=context['curpos'])
        query = urllib.parse.urlencode(dict(context=json.dumps(stripped)))
        return urllib.parse.urljoin('http://127.0.0.1:%s' % self._httpd.server_port, '?%s' % query)

    def shutdown(self,wait=True):
        """
        Shutdown the file server
        """
        self._httpd.shutdown()
        if wait:
            self.join()


def main():

    start_type = sys.argv[1]

    if start_type == 'core':

        # use the module name here
        setup_logging('cm_core')
        logger = logging.getLogger(__name__)
        logger.setLevel(get_loglevel())

        # change proccess title
        try:
            import setproctitle
            setproctitle.setproctitle('nvim-completion-manager core')
        except:
            pass

        try:
            # connect neovim
            nvim = attach('stdio')
            handler = Handler(nvim)
            logger.info('starting core, enter event loop')
            cm_event_loop('core',logger,nvim,handler)
        except Exception as ex:
            logger.info('Exception: %s',ex)

    elif start_type == 'channel':

        path = sys.argv[2]
        dir = os.path.dirname(path)
        name = os.path.splitext(os.path.basename(path))[0]

        # use the module name here
        setup_logging(name)
        logger = logging.getLogger(name)
        logger.setLevel(get_loglevel())

        # change proccess title
        try:
            import setproctitle
            setproctitle.setproctitle('nvim-completion-manager channel %s' % name)
        except:
            pass


        try:
            # connect neovim
            nvim = attach('stdio')
            sys.path.append(dir)
            m = importlib.import_module(name)
            handler = m.Handler(nvim)
            logger.info('handler created, entering event loop')
            cm_event_loop('channel',logger,nvim,handler)
        except Exception as ex:
            logger.info('Exception: %s',ex)

def get_loglevel():
    # logging setup
    level = logging.INFO
    if 'NVIM_PYTHON_LOG_LEVEL' in os.environ:
        l = getattr(logging,
                os.environ['NVIM_PYTHON_LOG_LEVEL'].strip(),
                level)
        if isinstance(l, int):
            level = l
    return level


def cm_event_loop(type,logger,nvim,handler):

    def on_setup():
        logger.info('on_setup')

    def on_request(method, args):

        func = getattr(handler,method,None)
        if func is None:
            logger.info('method: %s not implemented, ignore this request', method)
            return None

        func(*args)

    def on_notification(method, args):
        logger.info('%s method: %s, args: %s', type, method, args)

        if type=='channel' and method=='cm_refresh':
            ctx = args[1]
            # The refresh calculation may be heavy, and the notification queue
            # may have outdated refresh events, it would be  meaningless to
            # process these event
            if nvim.call('cm#context_changed',ctx):
                logger.info('context_changed, ignoring context: %s', ctx)
                return

        func = getattr(handler,method,None)
        if func is None:
            logger.info('method: %s not implemented, ignore this message', method)
            return

        func(*args)

    nvim.run_loop(on_request, on_notification, on_setup)

    # shutdown
    func = getattr(handler,'cm_shutdown',None)
    if func:
        func()


main()

