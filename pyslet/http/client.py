#! /usr/bin/env python

import logging
import string
import time
import socket
import ssl
import select
import types
import threading
import io
import warnings
import errno
import os

import pyslet.info as info
import pyslet.http.grammar as grammar
import pyslet.http.params as params
import pyslet.http.messages as messages
import pyslet.http.auth as auth
import pyslet.rfc2396 as uri


class RequestManagerBusy(messages.HTTPException):

    """Raised when attempting to queue a request and no connections
    become available within the specified timeout."""
    pass


class ConnectionClosed(messages.HTTPException):

    """Raised when attempting to queue a request when the manager object
    is in the process of closing."""
    pass

HTTP_PORT = 80      #: symbolic name for the default HTTP port
HTTPS_PORT = 443        #: symbolic name for the default HTTPS port


SOCKET_CHUNK = io.DEFAULT_BUFFER_SIZE
"""The default chunk size to use when reading from network sockets."""

USER_AGENT = params.ProductToken('pyslet', info.version)
"""A :py:class:`ProductToken` instance that can be used to represent the
current version of Pyslet."""


class Connection(object):

    """Represents an HTTP connection.  Used internally by the request
    manager to manage connections to HTTP servers.  Each connection is
    assigned a unique :py:attr:`id` on construction.  In normal use you
    won't need to call any of these methods yourself but the interfaces
    are documented to make it easier to override the behaviour of the
    :py:class:`messages.Message` object that *may* call some of these
    connection methods to indicate protocol exceptions.

    Connections define comparison methods, if c1 and c2 are both
    instances then::

        c1 < c2 == True

    ...if c1 was last active before c2.  The connection's active time is
    updated each time :py:meth:`connection_task` is called.

    Connections are shared across threads but are never in use by more
    than one thread at a time.  The thread currently bound to a
    connection is indicated by :py:attr:`thread_id`.  The value of this
    attribute is managed by the associated
    :py:class:`HTTPRequestManager`. Methods *must only* be called
    from this thread unless otherwise stated.

    The scheme, hostname and port are defined on construction and do not
    change."""
    REQ_READY = 0           # ready to start a request
    REQ_BODY_WAITING = 1    # waiting to send the request body
    REQ_BODY_SENDING = 2    # sending the request body
    CLOSE_WAIT = 3          # waiting to disconnect

    MODE_STRINGS = {0: "Ready", 1: "Waiting", 2: "Sending", 3: "Closing"}

    IDEMPOTENT = {"GET": 1, "HEAD": 1, "PUT": 1, "DELETE": 1,
                  "OPTIONS": 1, "TRACE": 1, "CONNECT": 0, "POST": 0}

    def __init__(self, manager, scheme, hostname, port, timeout=None):
        #: the RequestManager that owns this connection
        self.manager = manager
        #: the id of this connection object
        self.id = self.manager._nextid()
        #: the http scheme in use, 'http' or 'https'
        self.scheme = scheme
        #: the target host of this connection
        self.host = hostname
        #: the target port of this connection
        self.port = port
        #: the protocol version of the last response from the server
        self.protocol = None
        #: the thread we're currently bound to
        self.thread_id = None
        #: time at which this connection was last active
        self.lastActive = 0
        #: timeout (seconds) for our connection
        self.timeout = timeout
        #: time of the last successful read or write operation
        self.last_rw = None
        #: the queue of requests we are waiting to process
        self.requestQueue = []
        #: the current request we are processing
        self.request = None
        #: the queue of responses we are waiting to process
        self.responseQueue = []
        #: the current response we are processing
        self.response = None
        self.requestMode = self.REQ_READY
        # If we don't get a continue in 1 minute, send the data anyway
        self.continueWaitMax = 60.0
        self.continueWaitStart = 0
        # Low-level socket members
        self.connectionLock = threading.RLock()
        self.connectionClosed = False
        self.socket = None
        self.socketFile = None
        self.sendBuffer = []
        self.recvBuffer = []
        self.recvBufferSize = 0

    def thread_target_key(self):
        return (self.thread_id, self.scheme, self.host, self.port)

    def target_key(self):
        return (self.scheme, self.host, self.port)

    def __cmp__(self, other):
        if not isinstance(other, Connection):
            raise TypeError
        return cmp(self.lastActive, other.lastActive)

    def __repr__(self):
        return "Connection(%s,%i)" % (self.host, self.port)

    def connection_task(self):
        """Processes the requests and responses for this connection.

        This method is mostly non-blocking.  It returns a (r,w) pair of
        file numbers suitable for passing to select indicating whether
        the connection is waiting to read and/or write data.  It will
        return None,None if the connection is not currently blocked on
        I/O.

        The connection object acts as a small buffer between the HTTP
        message itself and the server.  The implementation breaks down
        in to a number of phases:

        1.  Start processing a request if one is queued and we're ready
            for it.  For idempotent requests (in practice, everything
            except POST) we take advantage of HTTP pipelining to send
            the request without waiting for the previous response(s).

            The only exception is when the request has an Expect:
            100-continue header.  In this case the pipeline stalls until
            the server has caught up with us and sent the 100 response
            code.

        2.  Send as much data to the server as we can without blocking.

        3.  Read and process as much data from the server as we can
            without blocking.

        The above steps are repeated until we are blocked at which point
        we return.

        Although data is streamed in a non-blocking manner there are
        situations in which the method will block.  DNS name resolution
        and creation/closure of sockets may block."""
        rbusy = None
        wbusy = None
        while True:
            self.lastActive = time.time()
            if self.requestQueue and self.requestMode == self.REQ_READY:
                request = self.requestQueue[0]
                if (self.response is None or
                        self.IDEMPOTENT.get(request.method, False)):
                    # If we are waiting for a response we only accept
                    # idempotent methods
                    self.requestQueue = self.requestQueue[1:]
                self._start_request(request)
            if self.request or self.response:
                if self.socket is None:
                    self.new_socket()
                rbusy = None
                wbusy = None
                # The first section deals with the sending cycle, we
                # pass on to the response section only if we are in a
                # waiting mode or we are waiting for the socket to be
                # ready before we can write data
                if self.sendBuffer:
                    try:
                        r, w, e = self.socketSelect(
                            [], [self.socketFile], [], 0.0)
                    except select.error, err:
                        self.close(err)
                        w = []
                    if w:
                        # We can write
                        self._send_request_data()
                    else:
                        if (self.last_rw is not None and
                                self.timeout is not None and
                                self.last_rw + self.timeout < time.time()):
                            # assume we're dead in the water
                            raise IOError(
                                errno.ETIMEDOUT,
                                os.strerror(errno.ETIMEDOUT),
                                "pyslet.http.client.Connection")
                    if self.sendBuffer:
                        # We are still waiting to write, move on to the
                        # response section!
                        wbusy = self.socketFile
                    else:
                        continue
                elif self.requestMode == self.REQ_BODY_WAITING:
                    # empty buffer and we're waiting for a 100-continue (that
                    # may never come)
                    if self.continueWaitStart:
                        if (time.time() - self.continueWaitStart >
                                self.continueWaitMax):
                            logging.warn("%s timeout while waiting for "
                                         "100-Continue response")
                            self.requestMode = self.REQ_BODY_SENDING
                    else:
                        self.continueWaitStart = time.time()
                elif self.requestMode == self.REQ_BODY_SENDING:
                    # Buffer is empty, refill it from the request
                    data = self.request.send_body()
                    if data:
                        logging.debug("Sending to %s: \n%s", self.host, data)
                        self.sendBuffer.append(data)
                        # Go around again to send the buffer
                        continue
                    elif data is None:
                        logging.debug("send_body blocked "
                                      "waiting for message body")
                        # continue on to the response section
                    else:
                        # Buffer is empty, request is exhausted, we're
                        # done with it! we might want to tell the
                        # associated respone that it is now waiting, but
                        # matching is hard when pipelining!
                        # self.response.StartWaiting()
                        self.request.disconnect()
                        self.request = None
                        self.requestMode = self.REQ_READY
                # This section deals with the response cycle, we only
                # get here once the buffer is empty or we're blocked on
                # sending.
                if self.response:
                    try:
                        r, w, e = self.socketSelect(
                            [self.socketFile], [], [self.socketFile], 0)
                    except select.error, err:
                        r = []
                        self.close(err)
                    if e:
                        # there is an error on our socket...
                        self.close("socket error indicated by select")
                    elif r:
                        if self._recv_task():
                            # The response is done
                            close_connection = False
                            if self.response:
                                self.protocol = self.response.protocol
                                close_connection = not self.response.keep_alive
                            if self.responseQueue:
                                self.response = self.responseQueue[0]
                                self.responseQueue = self.responseQueue[1:]
                                self.response.start_receiving()
                            elif self.response:
                                self.response = None
                                if self.requestMode == self.CLOSE_WAIT:
                                    # no response and waiting to close the
                                    # connection
                                    close_connection = True
                            if close_connection:
                                self.close()
                        # Any data received on the connection could
                        # change the request state, so we loop round
                        # again
                        continue
                    else:
                        if (self.last_rw is not None and
                                self.timeout is not None and
                                self.last_rw + self.timeout < time.time()):
                            # assume we're dead in the water
                            raise IOError(
                                errno.ETIMEDOUT,
                                os.strerror(errno.ETIMEDOUT),
                                "pyslet.http.client.Connection")
                        rbusy = self.socketFile
                break
            else:
                # no request or response, we're idle
                if self.requestMode == self.CLOSE_WAIT:
                    # clean up if necessary
                    self.close()
                self.manager._deactivate_connection(self)
                rbusy = None
                wbusy = None
                break
        return rbusy, wbusy

    def request_disconnect(self):
        """Disconnects the connection, aborting the current request."""
        self.request.disconnect()
        self.request = None
        if self.response:
            self.sendBuffer = []
            self.requestMode = self.CLOSE_WAIT
        else:
            self.close()

    def continue_sending(self, request):
        """Instructs the connection to start sending any pending request body.

        If a request had an "Expect: 100-continue" header then the
        connection will not send the data until instructed to do so by a
        call to this method, or
        :py:attr:`continueWaitMax` seconds have elapsed."""
        logging.debug("100 Continue received... ready to send request")
        if (request is self.request and
                self.requestMode == self.REQ_BODY_WAITING):
            self.requestMode = self.REQ_BODY_SENDING

    def close(self, err=None):
        """Closes this connection nicelly, optionally logging the
        exception *err*

        The connection disconnects from the current request and
        terminates any responses we are waiting for by calling their
        :py:meth:`ClientResponse.handle_disconnect` methods.

        Finally, the socket is closed and all internal structures are
        reset ready to reconnect when the next request is queued."""
        if err:
            logging.error(
                "%s: closing connection after error %s", self.host, str(err))
        else:
            logging.debug("%s: closing connection", self.host)
        if self.request:
            self.request.disconnect()
            self.request = None
            self.requestMode = self.CLOSE_WAIT
        while self.response:
            # If we get Closed while waiting for a response then we tell
            # the response about the error before hanging up
            self.response.handle_disconnect(err)
            if self.responseQueue:
                self.response = self.responseQueue[0]
                self.responseQueue = self.responseQueue[1:]
            else:
                self.response = None
        with self.connectionLock:
            if self.socket:
                olds = self.socket
                self.socket = None
                if olds is not None:
                    self._close_socket(olds)
        self.sendBuffer = []
        self.recvBuffer = []
        self.recvBufferSize = 0
        self.requestMode = self.REQ_READY

    def kill(self):
        """Kills the connection, typically called from a different
        thread than the one currently bound (if any).

        No request methods are invoked, it is assumed that after this
        method the manager will relinquish control of the connection
        object creating space in the pool for other connections.  Once
        killed, a connection is never reconnected.

        If the owning thread calls connection_task after kill completes
        it will get a socket error or unexpectedly get zero-bytes on
        recv indicating the connection is broken.  We don't close the
        socket here, just shut it down to be nice to the server.

        If the owning thread really died, Python's garbage collection
        will take care of actually closing the socket and freeing up the
        file descriptor."""
        with self.connectionLock:
            logging.debug("Killing connection to %s", self.host)
            if not self.connectionClosed and self.socket:
                try:
                    logging.warn(
                        "Connection.kill forcing socket shutdown for %s",
                        self.host)
                    self.socket.shutdown(socket.SHUT_RDWR)
                except socket.error:
                    # ignore errors, most likely the server has stopped
                    # listening
                    pass
                self.connectionClosed = True

    def _start_request(self, request):
        # Starts processing the request.  Returns True if the request
        # has been accepted for processing, False otherwise.
        self.request = request
        self.request.set_connection(self)
        self.request.start_sending(self.protocol)
        headers = self.request.send_start() + self.request.send_header()
        logging.debug("Sending to %s: \n%s", self.host, headers)
        self.sendBuffer.append(headers)
        # Now check to see if we have an expect header set
        if self.request.get_expect_continue():
            self.requestMode = self.REQ_BODY_WAITING
            self.continueWaitStart = 0
        else:
            self.requestMode = self.REQ_BODY_SENDING
        logging.debug("%s: request mode=%s", self.host,
                      self.MODE_STRINGS[self.requestMode])
        if self.response:
            # Queue a response as we're still handling the last one!
            self.responseQueue.append(request.response)
        else:
            self.response = request.response
            self.response.start_receiving()
        return True

    def _send_request_data(self):
        #   Sends the next chunk of data in the buffer
        if not self.sendBuffer:
            return
        data = self.sendBuffer[0]
        if data:
            try:
                nbytes = self.socket.send(data)
                self.last_rw = time.time()
            except socket.error, err:
                # stop everything
                self.close(err)
                return
            if nbytes == 0:
                # We can't send any more data to the socket
                # The other side has closed the connection
                # Strangely, there is nothing much to do here,
                # if the server fails to send a response that
                # will be handled more seriously.  However,
                # we do change to a mode that prevents future
                # requests!
                self.request.disconnect()
                self.request = None
                self.requestMode == self.CLOSE_WAIT
                self.sendBuffer = []
            elif nbytes < len(data):
                # Some of the data went:
                self.sendBuffer[0] = data[nbytes:]
            else:
                del self.sendBuffer[0]
        else:
            # shouldn't get empty strings in the buffer but if we do, delete
            # them
            del self.sendBuffer[0]

    def _recv_task(self):
        #   We ask the response what it is expecting and try and
        #   satisfy that, we return True when the response has been
        #   received completely, False otherwise"""
        err = None
        try:
            data = self.socket.recv(SOCKET_CHUNK)
            self.last_rw = time.time()
        except socket.error, e:
            # We can't truly tell if the server hung-up except by
            # getting an error here so this error could be fairly benign.
            err = e
            data = None
        logging.debug("Reading from %s: \n%s", self.host, repr(data))
        if data:
            nbytes = len(data)
            self.recvBuffer.append(data)
            self.recvBufferSize += nbytes
        else:
            # TODO: this is typically a signal that the other end hung
            # up, we should implement the HTTP retry strategy for the
            # related request
            logging.debug("%s: closing connection after recv returned no "
                          "data on ready to read socket", self.host)
            self.close()
            return True
        # Now loop until we can't satisfy the response anymore (or the response
        # is done)
        while self.response is not None:
            recv_needs = self.response.recv_mode()
            if recv_needs is None:
                # We don't need any bytes at all, the response is done
                return True
            elif recv_needs == messages.Message.RECV_HEADERS:
                # scan for CRLF, consolidate first
                data = string.join(self.recvBuffer, '')
                pos = data.find(grammar.CRLF)
                if pos == 0:
                    # just a blank line, no headers
                    lines = [grammar.CRLF]
                    data = data[2:]
                elif pos > 0:
                    # we need CRLFCRLF actually
                    pos = data.find(grammar.CRLF + grammar.CRLF)
                    # pos can't be 0 now...
                if pos > 0:
                    # split the data into lines
                    lines = map(
                        lambda x: x + grammar.CRLF,
                        data[0:pos + 2].split(grammar.CRLF))
                    data = data[pos + 4:]
                elif err:
                    self.close(err)
                    return True
                elif pos < 0:
                    # We didn't find the data we wanted this time
                    break
                if data:
                    self.recvBuffer = [data]
                    self.recvBufferSize = len(data)
                else:
                    self.recvBuffer = []
                    self.recvBufferSize = 0
                if lines:
                    logging.debug("Response Headers: %s", repr(lines))
                    self.response.recv(lines)
            elif recv_needs == messages.Message.RECV_LINE:
                # scan for CRLF, consolidate first
                data = string.join(self.recvBuffer, '')
                pos = data.find(grammar.CRLF)
                if pos >= 0:
                    line = data[0:pos + 2]
                    data = data[pos + 2:]
                elif err:
                    self.close(err)
                    return True
                else:
                    # We didn't find the data we wanted this time
                    break
                if data:
                    self.recvBuffer = [data]
                    self.recvBufferSize = len(data)
                else:
                    self.recvBuffer = []
                    self.recvBufferSize = 0
                if line:
                    logging.debug("Response Header: %s", repr(line))
                    self.response.recv(line)
            elif recv_needs == 0:
                # we're blocked
                logging.debug("Response blocked on write")
                self.response.recv(None)
            else:
                nbytes = int(recv_needs)
                if nbytes < 0:
                    # As many as possible please
                    logging.debug("Response reading until connection closes")
                    if self.recvBufferSize > 0:
                        bytes = string.join(self.recvBuffer, '')
                        self.recvBuffer = []
                        self.recvBufferSize = 0
                    else:
                        # recvBuffer is empty but we still want more
                        break
                elif self.recvBufferSize < nbytes:
                    logging.debug("Response waiting for %s bytes",
                                  str(nbytes - self.recvBufferSize))
                    # We can't satisfy the response
                    break
                else:
                    got_bytes = 0
                    buff_pos = 0
                    while got_bytes < nbytes:
                        data = self.recvBuffer[buff_pos]
                        if got_bytes + len(data) < nbytes:
                            buff_pos += 1
                            got_bytes += len(data)
                            continue
                        elif got_bytes + len(data) == nbytes:
                            bytes = string.join(
                                self.recvBuffer[0:buff_pos + 1], '')
                            self.recvBuffer = self.recvBuffer[buff_pos + 1:]
                            break
                        else:
                            # Tricky case, only some of this string is needed
                            bytes = string.join(self.recvBuffer[0:buff_pos] +
                                                [data[0:nbytes - got_bytes]],
                                                '')
                            self.recvBuffer = ([data[nbytes - got_bytes:]] +
                                               self.recvBuffer[buff_pos + 1:])
                            break
                    self.recvBufferSize = self.recvBufferSize - len(bytes)
                logging.debug("Response Data: %s", repr(bytes))
                self.response.recv(bytes)
        return False

    def new_socket(self):
        with self.connectionLock:
            if self.connectionClosed:
                logging.error(
                    "new_socket called on dead connection to %s", self.host)
                raise messages.HTTPException("Connection closed")
            self.socket = None
            self.socketFile = None
            self.socketSelect = select.select
        try:
            for target in self.manager.dnslookup(self.host, self.port):
                family, socktype, protocol, canonname, address = target
                try:
                    snew = socket.socket(family, socktype, protocol)
                    snew.connect(address)
                except socket.error:
                    if snew:
                        snew.close()
                        snew = None
                    continue
                break
        except socket.gaierror, e:
            snew = None
            raise messages.HTTPException(
                "failed to connect to %s (%s)" % (self.host, e[1]))
        if not snew:
            raise messages.HTTPException("failed to connect to %s" % self.host)
        else:
            with self.connectionLock:
                if self.connectionClosed:
                    # This connection has been killed
                    self._close_socket(snew)
                    logging.error(
                        "Connection killed while connecting to %s", self.host)
                    raise messages.HTTPException("Connection closed")
                else:
                    self.socket = snew
                    self.socketFile = self.socket.fileno()
                    self.socketSelect = select.select

    def _close_socket(self, s):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except socket.error:
            # ignore errors, most likely the server has stopped listening
            pass
        try:
            s.close()
        except socket.error:
            pass


class SecureConnection(Connection):

    def __init__(self, manager, scheme, hostname, port, ca_certs=None):
        super(SecureConnection, self).__init__(manager, scheme, hostname, port)
        self.ca_certs = ca_certs

    def new_socket(self):
        super(SecureConnection, self).new_socket()
        try:
            with self.connectionLock:
                if self.socket is not None:
                    socket_ssl = ssl.wrap_socket(
                        self.socket, ca_certs=self.ca_certs,
                        cert_reqs=ssl.CERT_REQUIRED if
                        self.ca_certs is not None else ssl.CERT_NONE)
                    # self.socket_ssl=socket.ssl(self.socket)
                    self.socketTransport = self.socket
                    self.socket = socket_ssl
                    logging.info(
                        "Connected to %s with %s, %s, key length %i",
                        self.host, *self.socket.cipher())
        except socket.error:
            raise messages.HTTPException(
                "failed to build secure connection to %s" % self.host)


class HTTPRequestManager(object):

    """An object for managing the sending of HTTP/1.1 requests and
    receiving of responses.  There are a number of keyword arguments
    that can be used to set operational parameters:

    max_connections
        The maximum number of HTTP connections that may be open at any
        one time.  The method :py:meth:`queue_request` will block (or
        raise :py:class:`RequestManagerBusy`) if an attempt to queue a
        request would cause this limit to be exceeded.

    ca_certs
        The file name of a certificate file to use when checking SSL
        connections.  For more information see
        http://docs.python.org/2.7/library/ssl.html

    .. warning::

        By default, ca_certs is optional and can be passed as None.  In
        this mode certificates will not be checked and your connections
        are not secure from man in the middle attacks.  In production
        use you should always specify a certificate file if you expect
        to use the object to make calls to https URLs.

    Although max_connections allows you to make multiple connections to
    the same host+port the request manager imposes an additional
    restriction. Each thread can make at most 1 connection to each
    host+port.  If multiple requests are made to the same host+port from
    the same thread then they are queued and will be sent to the server
    over the same connection using HTTP/1.1 pipelining. The manager
    (mostly) takes care of the following restriction imposed by RFC2616:

        Clients SHOULD NOT pipeline requests using non-idempotent
        methods or non-idempotent sequences of methods

    In other words, a POST  (or CONNECT) request will cause the
    pipeline to stall until all the responses have been received.  Users
    should beware of non-idempotent sequences as these are not
    automatically detected by the manager.  For example, a GET,PUT
    sequence on the same resource is not idempotent. Users should wait
    for the GET request to finish fetching the resource before queuing a
    PUT request that overwrites it.

    In summary, to take advantage of multiple simultaneous connections
    to the same host+port you must use multiple threads."""
    ConnectionClass = Connection
    SecureConnectionClass = SecureConnection

    def __init__(self, max_connections=100, ca_certs=None):
        self.managerLock = threading.Condition()
        # the id of the next connection object we'll create
        self.nextId = 1
        self.cActiveThreadTargets = {}
        # A dict of active connections keyed on thread and target (always
        # unique)
        self.cActiveThreads = {}
        # A dict of dicts of active connections keyed on thread id then
        # connection id
        self.cIdleTargets = {}
        # A dict of dicts of idle connections keyed on target and then
        # connection id
        self.cIdleList = {}
        # A dict of idle connections keyed on connection id (for keeping count)
        self.closing = False                    # True if we are closing
        # maximum number of connections to manage (set only on construction)
        self.max_connections = max_connections
        # cached results from socket.getaddrinfo keyed on (hostname,port)
        self.dnsCache = {}
        self.ca_certs = ca_certs
        self.credentials = []
        self.socketSelect = select.select
        self.httpUserAgent = "%s (HTTPRequestManager)" % str(USER_AGENT)
        """The default User-Agent string to use."""

    def QueueRequest(self, request, timeout=60):    # noqa
        warnings.warn("HTTPRequestManager.QueueRequest is deprecated, "
                      "use HTTPRequestManager.queue_request instead",
                      DeprecationWarning,
                      stacklevel=2)
        return self.queue_request(request, timeout)

    def queue_request(self, request, timeout=None):
        """Instructs the manager to start processing *request*.

        request
            A :py:class:`messages.Message` object.

        timeout
            Number of seconds to wait for a free connection before
            timing out.  A timeout raises :py:class:`RequestManagerBusy`

            None means wait forever, 0 means don't block.

        The default implementation adds a User-Agent header from
        :py:attr:`httpUserAgent` if none has been specified already.
        You can override this method to add other headers appropriate
        for a specific context but you must pass this call on to this
        implementation for proper processing."""
        if self.httpUserAgent and not request.has_header('User-Agent'):
            request.set_header('User-Agent', self.httpUserAgent)
        # assign this request to a connection straight away
        start = time.time()
        thread_id = threading.current_thread().ident
        thread_target = (
            thread_id, request.scheme, request.hostname, request.port)
        target = (request.scheme, request.hostname, request.port)
        with self.managerLock:
            if self.closing:
                raise ConnectionClosed
            while True:
                # Step 1: search for an active connection to the same
                # target already bound to our thread
                if thread_target in self.cActiveThreadTargets:
                    connection = self.cActiveThreadTargets[thread_target]
                    break
                # Step 2: search for an idle connection to the same
                # target and bind it to our thread
                elif target in self.cIdleTargets:
                    cidle = self.cIdleTargets[target].values()
                    cidle.sort()
                    # take the youngest connection
                    connection = cidle[-1]
                    self._activate_connection(connection, thread_id)
                    break
                # Step 3: create a new connection
                elif (len(self.cActiveThreadTargets) + len(self.cIdleList) <
                      self.max_connections):
                    connection = self._new_connection(target)
                    self._activate_connection(connection, thread_id)
                    break
                # Step 4: delete the oldest idle connection and go round again
                elif len(self.cIdleList):
                    cidle = self.cIdleList.values()
                    cidle.sort()
                    connection = cidle[0]
                    self._delete_idle_connection(connection)
                # Step 5: wait for something to change
                else:
                    now = time.time()
                    if timeout == 0:
                        logging.warn(
                            "non-blocking call to queue_request failed to "
                            "obtain an HTTP connection")
                        raise RequestManagerBusy
                    elif timeout is not None and now > start + timeout:
                        logging.warn(
                            "queue_request timed out while waiting for "
                            "an HTTP connection")
                        raise RequestManagerBusy
                    logging.debug(
                        "queue_request forced to wait for an HTTP connection")
                    self.managerLock.wait(timeout)
                    logging.debug(
                        "queue_request resuming search for an HTTP connection")
            # add this request tot he queue on the connection
            connection.requestQueue.append(request)
            request.set_client(self)

    def active_count(self):
        """Returns the total number of active connections."""
        with self.managerLock:
            return len(self.cActiveThreadTargets)

    def thread_active_count(self):
        """Returns the total number of active connections associated
        with the current thread."""
        thread_id = threading.current_thread().ident
        with self.managerLock:
            return len(self.cActiveThreads.get(thread_id, {}))

    def _activate_connection(self, connection, thread_id):
        # safe if connection is new and not in the idle list
        connection.thread_id = thread_id
        target = connection.target_key()
        thread_target = connection.thread_target_key()
        with self.managerLock:
            self.cActiveThreadTargets[thread_target] = connection
            if thread_id in self.cActiveThreads:
                self.cActiveThreads[thread_id][connection.id] = connection
            else:
                self.cActiveThreads[thread_id] = {connection.id: connection}
            if connection.id in self.cIdleList:
                del self.cIdleList[connection.id]
                del self.cIdleTargets[target][connection.id]
                if not self.cIdleTargets[target]:
                    del self.cIdleTargets[target]

    def _deactivate_connection(self, connection):
        # called when connection goes idle, it is possible that this
        # connection has been killed and just doesn't know it (like
        # Bruce Willis in Sixth Sense) so we take care to return it
        # to the idle pool only if it was in the active pool
        target = connection.target_key()
        thread_target = connection.thread_target_key()
        with self.managerLock:
            if thread_target in self.cActiveThreadTargets:
                del self.cActiveThreadTargets[thread_target]
                self.cIdleList[connection.id] = connection
                if target in self.cIdleTargets:
                    self.cIdleTargets[target][connection.id] = connection
                else:
                    self.cIdleTargets[target] = {connection.id: connection}
                # tell any threads waiting for a connection
                self.managerLock.notify()
            if connection.thread_id in self.cActiveThreads:
                if connection.id in self.cActiveThreads[connection.thread_id]:
                    del self.cActiveThreads[
                        connection.thread_id][connection.id]
                if not self.cActiveThreads[connection.thread_id]:
                    del self.cActiveThreads[connection.thread_id]
            connection.thread_id = None

    def _delete_idle_connection(self, connection):
        if connection.id in self.cIdleList:
            target = connection.target_key()
            del self.cIdleList[connection.id]
            del self.cIdleTargets[target][connection.id]
            if not self.cIdleTargets[target]:
                del self.cIdleTargets[target]
            connection.close()

    def _nextid(self):
        #   Used internally to manage auto-incrementing connection ids
        with self.managerLock:
            id = self.nextId
            self.nextId += 1
        return id

    def _new_connection(self, target, timeout=None):
        #   Called by a connection pool when a new connection is required
        scheme, host, port = target
        if scheme == 'http':
            connection = self.ConnectionClass(self, scheme, host, port)
        elif scheme == 'https':
            connection = self.SecureConnectionClass(
                self, scheme, host, port, self.ca_certs)
        else:
            raise NotImplementedError(
                "Unsupported connection scheme: %s" % scheme)
        return connection

    def thread_task(self, timeout=None):
        """Processes all connections bound to the current thread then
        blocks for at most timeout (0 means don't block) while waiting
        to send/receive data from any active sockets.

        Each active connection receives one call to
        :py:meth:`Connection.connection_task` There are some situations
        where this method may still block even with timeout=0.  For
        example, DNS name resolution and SSL handshaking.  These may be
        improved in future.

        Returns True if at least one connection is active, otherwise
        returns False."""
        thread_id = threading.current_thread().ident
        with self.managerLock:
            connections = self.cActiveThreads.get(thread_id, {}).values()
        if not connections:
            return False
        readers = []
        writers = []
        for c in connections:
            try:
                r, w = c.connection_task()
                if r:
                    readers.append(r)
                if w:
                    writers.append(w)
            except messages.HTTPException as err:
                c.close(err)
                pass
        if (timeout is None or timeout > 0) and (readers or writers):
            try:
                logging.debug("thread_task waiting for select: "
                              "readers=%s, writers=%s, timeout=%i",
                              repr(readers), repr(writers), timeout)
                r, w, e = self.socketSelect(readers, writers, [], timeout)
            except select.error, err:
                logging.error("Socket error from select: %s", str(err))
        return True

    def thread_loop(self, timeout=60):
        """Repeatedly calls :py:meth:`thread_task` until it returns False."""
        while self.thread_task(timeout):
            continue
        # self.close()

    def ProcessRequest(self, request, timeout=60):    # noqa
        warnings.warn("HTTPRequestManager.ProcessRequest is deprecated, "
                      "use HTTPRequestManager.process_request instead",
                      DeprecationWarning,
                      stacklevel=2)
        return self.process_request(request, timeout)

    def process_request(self, request, timeout=60):
        """Process an :py:class:`messages.Message` object.

        The request is queued and then :py:meth:`thread_loop` is called
        to exhaust all HTTP activity initiated by the current thread."""
        self.queue_request(request, timeout)
        self.thread_loop(timeout)

    def idle_cleanup(self, max_inactive=15):
        """Cleans up any idle connections that have been inactive for
        more than *max_inactive* seconds."""
        clist = []
        now = time.time()
        with self.managerLock:
            for connection in self.cIdleList.values():
                if connection.lastActive < now - max_inactive:
                    clist.append(connection)
                    del self.cIdleList[connection.id]
                    target = connection.target_key()
                    if target in self.cIdleTargets:
                        del self.cIdleTargets[target][connection.id]
                        if not self.cIdleTargets[target]:
                            del self.cIdleTargets[target]
        # now we can clean up these connections in a more leisurely fashion
        if clist:
            logging.debug("idle_cleanup closing connections...")
            for connection in clist:
                connection.close()

    def active_cleanup(self, max_inactive=90):
        """Clean up active connections that have been inactive for
        more than *max_inactive* seconds.

        This method can be called from any thread and can be used to
        remove connections that have been abandoned by their owning
        thread.  This can happen if the owning thread stops calling
        :py:meth:`thread_task` leaving some connections active.

        Inactive connections are killed using :py:meth:`Connection.kill`
        and then removed from the active list.  Should the owning thread
        wake up and attempt to finish processing the requests a socket
        error or :py:class:`messages.HTTPException` will be reported."""
        clist = []
        now = time.time()
        with self.managerLock:
            for thread_id in self.cActiveThreads:
                for connection in self.cActiveThreads[thread_id].values():
                    if connection.lastActive < now - max_inactive:
                        # remove this connection from the active lists
                        del self.cActiveThreads[thread_id][connection.id]
                        del self.cActiveThreadTargets[
                            connection.thread_target_key()]
                        clist.append(connection)
            if clist:
                # if stuck threads were blocked waiting for a connection
                # then we can wake them up, one for each connection
                # killed
                self.managerLock.notify(len(clist))
        if clist:
            logging.debug("active_cleanup killing connections...")
            for connection in clist:
                connection.kill()

    def close(self):
        """Closes all connections and sets the manager to a state where
        new connections cannot not be created.

        Active connections are killed, idle connections are closed."""
        while True:
            with self.managerLock:
                self.closing = True
                if len(self.cActiveThreadTargets) + len(self.cIdleList) == 0:
                    break
            self.active_cleanup(0)
            self.idle_cleanup(0)

    def Close(self):    # noqa
        warnings.warn("HTTPRequestManager.Close is deprecated, use close",
                      DeprecationWarning,
                      stacklevel=2)
        return self.close()

    def add_credentials(self, credentials):
        """Adds a :py:class:`pyslet.rfc2617.Credentials` instance to this
        manager.

        Credentials are used in response to challenges received in HTTP
        401 responses."""
        with self.managerLock:
            self.credentials.append(credentials)

    def AddCredentials(self, credentials):  # noqa
        warnings.warn("HTTPRequestManager.AddCredentials is deprecated, use "
                      "add_credentials", DeprecationWarning, stacklevel=2)
        return self.add_credentials(credentials)

    def remove_credentials(self, credentials):
        """Removes credentials from this manager.

        credentials
            A :py:class:`pyslet.rfc2617.Credentials` instance previously
            added with :py:meth:`add_credentials`.

        If the credentials can't be found then they are silently ignored
        as it is possible that two threads may independently call the
        method with the same credentials."""
        with self.managerLock:
            for i in xrange(len(self.credentials)):
                if self.credentials[i] is credentials:
                    del self.credentials[i]

    def dnslookup(self, host, port):
        """Given a host name (string) and a port number performs a DNS lookup
        using the native socket.getaddrinfo function.  The resulting value is
        added to an internal dns cache so that subsequent calls for the same
        host name and port do not use the network unnecessarily.

        If you want to flush the cache you must do so manually using
        :py:meth:`flush_dns`."""
        with self.managerLock:
            result = self.dnsCache.get((host, port), None)
        if result is None:
            # do not hold the lock while we do the DNS lookup, this may
            # result in multiple overlapping DNS requests but this is
            # better than a complete block.
            logging.debug("Looking up %s", host)
            result = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            with self.managerLock:
                # blindly populate the cache
                self.dnsCache[(host, port)] = result
        return result

    def flush_dns(self):
        """Flushes the DNS cache."""
        with self.managerLock:
            self.dnsCache = {}

    def find_credentials(self, challenge):
        """Searches for credentials that match *challenge*"""
        logging.debug("HTTPRequestManager searching for credentials in "
                      "%s with challenge %s",
                      challenge.protectionSpace, str(challenge))
        with self.managerLock:
            for c in self.credentials:
                if c.match_challenge(challenge):
                    return c

    def find_credentials_by_url(self, url):
        """Searches for credentials that match *url*"""
        with self.managerLock:
            for c in self.credentials:
                if c.test_url(url):
                    return c


class ClientRequest(messages.Request):

    """Represents an HTTP request.

    To make an HTTP request, create an instance of this class and then
    pass it to an :py:class:`HTTPRequestManager` instance using either
    :py:meth:`HTTPRequestManager.queue_request` or
    :py:meth:`HTTPRequestManager.process_request`.

    url
        An absolute URI using either http or https schemes.  A
        :py:class:`pyslet.rfc2396.URI` instance or an object that can be
        passed to its constructor.

    method
        A string.  The HTTP method to use, defaults to "GET"

    entity_body
        A string or stream-like object containing the request body.
        Defaults to None meaning no message body.  For stream-like
        objects the tell and seek methods must be supported to enable
        resending the request if required.

    res_body
        A stream-like object to write data to.  Defaults to None, in
        which case the response body is returned as a string the
        :py:attr:`res_body`.

    protocol
        An :py:class:`params.HTTPVersion` object, defaults to
        HTTPVersion(1,1)"""

    def __init__(self, url, method="GET", res_body=None,
                 protocol=params.HTTP_1p1, **kwargs):
        super(ClientRequest, self).__init__(**kwargs)
        self.manager = None
        self.connection = None
        #: the status code received, 0 indicates a failed or unsent request
        self.status = 0
        #: If status==0, the error raised during processing
        self.error = None
        self.set_url(url)
        self.method = method        #: the method
        if type(protocol) in types.StringTypes:
            self.protocol = params.HTTPVersion.from_str(protocol)
        elif isinstance(protocol, params.HTTPVersion):
            self.protocol = protocol
        else:
            raise TypeError("illegal value for protocol")
        #: the response body received (only used if not streaming)
        self.res_body = ''
        if res_body is not None:
            # assume that the res_body is a stream like object
            self.resBodyStream = res_body
        else:
            self.resBodyStream = None
        # : flag indicating whether or not to auto-redirect 3xx responses
        self.autoRedirect = True
        self.tryCredentials = None
        #: the associated :py:class:`ClientResponse`
        self.response = ClientResponse(request=self)

    def resend(self, url=None):
        logging.info("Resending request to: %s", str(url))
        self.status = 0
        self.error = None
        if url is not None:
            self.set_url(url)
        self.manager.queue_request(self)

    def set_client(self, client):
        """Called when we are queued for processing.

        client
            an :py:class:`HTTPRequestManager` instance"""
        self.manager = client

    def set_connection(self, connection):
        """Called when we are assigned to an HTTPConnection"""
        self.connection = connection

    def disconnect(self):
        """Called when the connection has finished sending the
        request, may be before or after the response is received
        and handled!"""
        self.connection = None
        if self.status > 0:
            # The response has finished
            self.finished()

    def send_header(self):
        # Check authorization and add credentials if the manager has them
        if not self.has_header("Authorization"):
            credentials = self.manager.find_credentials_by_url(self.url)
            if credentials:
                self.set_authorization(credentials)
        return super(ClientRequest, self).send_header()

    def response_finished(self, err=None):
        self.status = self.response.status
        self.error = err
        if self.status is None:
            logging.error("Error receiving response, %s", str(self.error))
            self.status = 0
            self.finished()
        else:
            logging.info("Finished Response, status %i", self.status)
            if self.resBodyStream:
                self.resBodyStream.flush()
            else:
                self.res_body = self.response.entity_body.getvalue()
            if self.response.status >= 100 and self.response.status <= 199:
                """Received after a 100 continue or other 1xx status
                response, we may be waiting for the connection to call
                our send_body method.  We need to tell it not to
                wait any more!"""
                if self.connection:
                    self.connection.continue_sending(self)
                # We're not finished though, wait for the final response
                # to be sent. No need to reset as the 100 response
                # should not have a body
            elif self.connection:
                # The response was received before the connection
                # finished with us
                if self.status >= 300:
                    # Some type of error condition....
                    if isinstance(self.send_body(), str):
                        # There was more data to send in the request but we
                        # don't plan to send it so we have to hang up!
                        self.connection.request_disconnect()
                    # else, we were finished anyway... the connection will
                    # discover this itself
                elif self.response >= 200:
                    # For 2xx result codes we let the connection finish
                    # spooling and disconnect from us when it is done
                    pass
                else:
                    # A bad information response (with body) or a bad status
                    # code
                    self.connection.request_disconnect()
            else:
                # The request is already disconnected, we're done
                self.finished()

    def Finished(self):     # noqa
        warnings.warn("ClientRequest.Finished is deprecated, "
                      "use ClientRequest.finished",
                      DeprecationWarning,
                      stacklevel=2)
        return self.finished()

    def finished(self):
        """Called when we have a final response *and* have disconnected
        from the connection There is no guarantee that the server got
        all of our data, it might even have returned a 2xx series code
        and then hung up before reading the data, maybe it already had
        what it needed, maybe it thinks a 2xx response is more likely to
        make us go away.  Whatever.  The point is that you can't be sure
        that all the data was transmitted just because you got here and
        the server says everything is OK"""
        if self.tryCredentials is not None:
            # we were trying out some credentials, if this is not a 401 assume
            # they're good
            if self.status == 401:
                # we must remove these credentials, they matched the challenge
                # but still resulted in 401
                self.manager.remove_credentials(self.tryCredentials)
            else:
                if isinstance(self.tryCredentials, auth.BasicCredentials):
                    # path rule only works for BasicCredentials
                    self.tryCredentials.add_success_path(self.url.absPath)
            self.tryCredentials = None
        if (self.autoRedirect and self.status >= 300 and
                self.status <= 399 and
                (self.status != 302 or
                 self.method.upper() in ("GET", "HEAD"))):
            # If the 302 status code is received in response to a
            # request other than GET or HEAD, the user agent MUST NOT
            # automatically redirect the request unless it can be
            # confirmed by the user
            location = self.response.get_header("Location").strip()
            if location:
                url = uri.URIFactory.URI(location)
                if not url.host:
                    # This is an error but a common one (thanks IIS!)
                    location = location.Resolve(self.url)
                self.resend(location)
        elif self.status == 401:
            challenges = self.response.get_www_authenticate()
            for c in challenges:
                c.protectionSpace = self.url.GetCanonicalRoot()
                self.tryCredentials = self.manager.find_credentials(c)
                if self.tryCredentials:
                    self.set_authorization(self.tryCredentials)
                    self.resend()  # to the same URL


class ClientResponse(messages.Response):

    def __init__(self, request, **kwargs):
        super(ClientResponse, self).__init__(
            request=request, entity_body=request.resBodyStream, **kwargs)

    def handle_headers(self):
        """Hook for response header processing.

        This method is called when a set of response headers has been
        received from the server, before the associated data is
        received!  After this call, recv will be called zero or more
        times until handle_message or handle_disconnect is called
        indicating the end of the response.

        Override this method, for example, if you want to reject or
        invoke special processing for certain responses (e.g., based on
        size) before the data itself is received.  To abort the
        response, close the connection using
        :py:meth:`Connection.request_disconnect`.

        Override the :py:meth:`Finished` method instead to clean up and
        process the complete response normally."""
        logging.debug(
            "Request: %s %s %s", self.request.method, self.request.url,
            str(self.request.protocol))
        logging.debug(
            "Got Response: %i %s", self.status, self.reason)
        logging.debug("Response headers: %s", repr(self.headers))
        super(ClientResponse, self).handle_headers()

    def handle_message(self):
        """Hook for normal completion of response"""
        self.finished()
        super(ClientResponse, self).handle_message()

    def handle_disconnect(self, err):
        """Hook for abnormal completion of the response

        Called when the server disconnects before we've completed
        reading the response.  Note that if we are reading forever this
        may be expected behaviour and *err* may be None.

        We pass this information on to the request."""
        if err is not None:
            self.reason = str(err)
        self.request.response_finished(err)

    def finished(self):
        self.request.response_finished()
        if self.status >= 100 and self.status <= 199:
            # Re-read this response, we're not done!
            self.start_receiving()
