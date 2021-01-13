import logging
import tornado.websocket

from tornado.ioloop import IOLoop
from tornado.iostream import _ERRNO_CONNRESET
from tornado.util import errno_from_exception

BUF_SIZE = 32 * 1024
clients = {}  # {ip: {id: worker}}

zmodemszstart = b'rz\r**\x18B00000000000000\r\x8a'
zmodemend = b'**\x18B0800000000022d\r\x8a'
zmodemrzstart = b'rz waiting to receive.**\x18B0100000023be50\r\x8a'
zmodemcancel = b'\x18\x18\x18\x18\x18\x08\x08\x08\x08\x08'


def clear_worker(worker, clients):
    ip = worker.src_addr[0]
    workers = clients.get(ip)
    assert worker.id in workers
    workers.pop(worker.id)

    if not workers:
        clients.pop(ip)
        if not clients:
            clients.clear()


def recycle_worker(worker):
    if worker.handler:
        return
    logging.warning('Recycling worker {}'.format(worker.id))
    worker.close(reason='worker recycled')


class Worker(object):
    def __init__(self, loop, ssh, chan, dst_addr):
        self.loop = loop
        self.ssh = ssh
        self.chan = chan
        self.dst_addr = dst_addr
        self.fd = chan.fileno()
        self.id = str(id(self))
        self.data_to_dst = []
        self.handler = None
        self.mode = IOLoop.READ
        self.closed = False
        self.zmodem = False
        self.zmodemOO = False

    def __call__(self, fd, events):
        if events & IOLoop.READ:
            self.on_read()
        if events & IOLoop.WRITE:
            self.on_write()
        if events & IOLoop.ERROR:
            self.close(reason='error event occurred')

    def set_handler(self, handler):
        if not self.handler:
            self.handler = handler

    def update_handler(self, mode):
        if self.mode != mode:
            self.loop.update_handler(self.fd, mode)
            self.mode = mode
        if mode == IOLoop.WRITE:
            self.loop.call_later(0.1, self, self.fd, IOLoop.WRITE)

    def on_read(self):
        logging.debug('worker {} on read'.format(self.id))
        try:
            if self.zmodemOO:
                self.zmodemOO = False
                data = self.chan.recv(2)
                if not len(data):
                    return
                if data == b'OO':
                    self.handler.write_message(data, binary=True)
                else:
                    data += self.chan.recv(4096)
            else:
                data = self.chan.recv(4096)
                if not len(data):
                    return
            # print(f"read----{data}------")
            if self.zmodem:
                if zmodemend in data:
                    self.zmodem = False
                    self.zmodemOO = True

                if zmodemcancel in data:
                    self.zmodem = False
                    self.handler.write_message("\n")
                self.handler.write_message(data, binary=True)
            else:
                if zmodemszstart in data or zmodemrzstart in data:
                    self.zmodem = True
                    self.handler.write_message(data, binary=True)
                else:
                    str_data = data.decode('utf-8', 'ignore')
                    self.handler.write_message(str_data)

        except tornado.websocket.WebSocketClosedError:
            self.close(reason='websocket closed')
        except (OSError, IOError) as e:
            logging.error(e)
            if self.chan.closed or errno_from_exception(e) in _ERRNO_CONNRESET:
                self.close(reason='chan error on reading')
        else:
            logging.debug('{!r} from {}:{}'.format(data, *self.dst_addr))
            if not data:
                self.close(reason='chan closed')
                return

    def on_write(self):

        logging.debug('worker {} on write'.format(self.id))
        if not self.data_to_dst:
            return

        data = ''.join(self.data_to_dst)
        print('{!r} to {}:{}'.format(data, *self.dst_addr))
        logging.debug('{!r} to {}:{}'.format(data, *self.dst_addr))

        try:
            # 往ssh发送数据
            sent = self.chan.send(data)
        except (OSError, IOError) as e:
            logging.error(e)
            if self.chan.closed or errno_from_exception(e) in _ERRNO_CONNRESET:
                self.close(reason='chan error on writing')
            else:
                self.update_handler(IOLoop.WRITE)
        else:
            self.data_to_dst = []
            data = data[sent:]

            if data:
                self.data_to_dst.append(data)
                print("write----up-write")
                self.update_handler(IOLoop.WRITE)
            else:
                print("write----up-read")
                self.update_handler(IOLoop.READ)

    def close(self, reason=None):
        if self.closed:
            return
        self.closed = True

        logging.info('Closing worker {} with reason: {}'.format(
            self.id, reason))
        if self.handler:
            self.loop.remove_handler(self.fd)
            self.handler.close(reason=reason)
        self.chan.close()
        self.ssh.close()
        logging.info('Connection to {}:{} lost'.format(*self.dst_addr))

        clear_worker(self, clients)
        logging.debug(clients)
