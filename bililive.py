import logging
import asyncio
import aiohttp
import struct
import json
import time
import re
from pybililive.consts import (
    WS_HOST, WS_PORT, WS_URI,
    WS_HEADER_STRUCT,
    HEADER_LENGTH, MAGIC, VERSION, MAGIC_PARAM,
    HEART_BEAT, JOIN_CHANNEL,
    WS_OP_CONNECT_SUCCESS, WS_OP_HEARTBEAT_REPLY, WS_OP_MESSAGE,
    HEARTBEAT_DELAY,
    API_LIVE_BASE_URL, GET_REAL_ROOM_URI, CHECK_USER_LOGIN_URI, GET_USER_INFO_URI,
    LIVE_BASE_URL, SEND_DANMU_URI
)
from pybililive.utils import (
    random_user_id, build_cookie_with_str
)

logger = logging.getLogger('bili')
ws_struct = struct.Struct(WS_HEADER_STRUCT)


class BiliLive(object):
    __slots__ = ['raw_room_id', 'room_id', 'raw_cookie', 'user_cookie', '_user_id', '_user_name',
                 '_user_login_status', 'loop', 'csrf_token',
                 'session', '_ws', '_heart_beat_task', '_cmd_func', '_stop', 'ext_settings']

    def __init__(self, room_id, user_cookie=None, cmd_func_dict=None, loop=None,
                 connector=None, stop=None):
        cmd_func_dict = cmd_func_dict if cmd_func_dict else {}
        self.loop = loop if loop else asyncio.get_event_loop()
        connector = connector if connector else aiohttp.TCPConnector(loop=loop)

        self.raw_room_id = room_id
        self.room_id = room_id

        self.raw_cookie = user_cookie
        self.csrf_token = ''
        if isinstance(user_cookie, str):
            user_cookie = build_cookie_with_str(user_cookie)
            self.csrf_token = user_cookie.get('bili_jct')

        self._stop = stop if stop else lambda kwargs: False

        self.user_cookie = user_cookie
        self._user_id = None
        self._user_name = None
        self._user_login_status = False
        self.session = aiohttp.ClientSession(loop=loop, connector=connector,
                                             cookies=user_cookie)
        self._ws = None
        self._heart_beat_task = None
        # message cmd function
        self._cmd_func = cmd_func_dict
        self.ext_settings = {}
        # cmd example
        # DANMU_MSG, SEND_GIFT, LIVE, PREPARING, WELCOME, WELCOME_GUARD, GUARD_BUY, ROOM_BLOCK_MSG
        # SYS_GIFT, SPECIAL_GIFT

    async def get_real_room_id(self, room_id):
        real_room_id = room_id
        try:
            res = await self.session.get(
                r'http://{host}:{port}/{uri}'.format(
                    host=API_LIVE_BASE_URL,
                    port=80,
                    uri=GET_REAL_ROOM_URI
                ), params={'id': self.room_id})
            data = await res.json()
            real_room_id = data['data']['room_id']
        except Exception as e:
            logger.exception(e)
        finally:
            return real_room_id

    async def connect(self):
        try:
            self.room_id = await self.get_real_room_id(self.room_id)
            await self.check_user_login_status()
            async with self.session.ws_connect(
                    r'ws://{host}:{port}/{uri}'.format(
                        host=WS_HOST,
                        port=WS_PORT,
                        uri=WS_URI
                    )) as ws:
                self._ws = ws
                await self.send_join_room()
                self._heart_beat_task = asyncio.ensure_future(self.heart_beat())
                async for msg in ws:
                    if not self._ws:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await self.on_binary(msg.data)
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        self.on_close()
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        self.on_error()
        except Exception as e:
            logger.exception(e)

    async def reconnect(self):
        pass

    async def check_user_login_status(self):
        if not self.user_cookie:
            self._user_login_status = False
            self._user_id = random_user_id()
            return

        try:
            res = await self.session.get(
                r'http://{host}:{port}/{uri}'.format(
                    host=API_LIVE_BASE_URL,
                    port=80,
                    uri=CHECK_USER_LOGIN_URI
                ))
            data = await res.json()
            if data['msg'] == 'success':
                logger.info('{user_name} 登录成功'.format(user_name=data['data']['uname']))
                user_info = await self.get_user_info()
                self._user_id = user_info['mid']
                self._user_name = user_info['uname']
        except Exception as e:
            logger.exception(e)

    async def get_user_info(self):
        user_info = {}
        try:
            url = r'http://{host}/{uri}'.format(
                    host='api.bilibili.com',
                    uri=GET_USER_INFO_URI
                )
            logger.info(r'get_user_info {url}'.format(url=url))
            res = await self.session.get(url)
            data = await res.json()
            user_info = data['data']
        except Exception as e:
            logger.exception(e)
        finally:
            return user_info

    async def send_danmu(self, danmu, max_length=30, room_id=None, color=16777215, font_size=25, mode=1):
        try:
            if len(danmu) <= max_length:
                await self._send_danmu(danmu, color, font_size, room_id if room_id else self.room_id, mode)
            else:
                while len(danmu) > max_length:
                    danmu_split = danmu[:30]
                    danmu = danmu[30:]
                    await self._send_danmu(danmu_split, color, font_size, room_id if room_id else self.room_id, mode)
                    await asyncio.sleep(2)
                else:
                    await self._send_danmu(danmu, color, font_size, room_id if room_id else self.room_id, mode)
        except Exception as e:
            logger.exception(e)
            logger.error('弹幕 {} 发送失败'.format(danmu))
        else:
            logger.info('弹幕 {} 发送成功'.format(danmu))

    async def _send_danmu(self, danmu, color, font_size, room_id, mode):
        res = await self.session.post(
            r'http://{host}:{port}/{uri}'.format(
                host=LIVE_BASE_URL,
                port=80,
                uri=SEND_DANMU_URI
            ), data={
                'msg': danmu,
                'color': color,
                'fontsize': font_size,
                'roomid': room_id,
                'rnd': int(time.time()),
                'mode': mode,
                'bubble': 0,
                'csrf_token': self.csrf_token,
                'csrf': self.csrf_token
            })
        data = await res.json()
        if data['code'] != 0:
            raise print('弹幕{}发送失败 {}'.format(danmu, data))

    async def send_join_room(self):
        await self.send_socket_data(action=JOIN_CHANNEL,
                                    payload=json.dumps({'mid': random_user_id(), 'roomid': self.room_id}))

    async def send_socket_data(self, action, payload='',
                               magic=MAGIC, ver=VERSION, param=MAGIC_PARAM):
        try:
            payload = bytearray(payload, 'utf-8')
            packet_length = len(payload) + HEADER_LENGTH
            data = struct.pack(WS_HEADER_STRUCT, packet_length, magic, ver, action, param) + payload
            await self._ws.send_bytes(data)
        except Exception as e:
            logger.exception(e)

    async def heart_beat(self):
        while True:
            try:
                if self.stop():
                    logger.info('Stop client.')
                    self._ws.close()
                    self._ws = None
                    self.loop.stop()
                    break
                logger.debug("Sending heart beat.")
                await self.send_socket_data(action=HEART_BEAT)
                await asyncio.sleep(HEARTBEAT_DELAY)
            except Exception as e:
                logger.exception(e)

    def stop(self, *args, **kwargs):
        """
        Add your stop condition
        :return:
        """
        return self._stop(self)

    def on_error(self):
        """
        Generally speaking, on_close will be invoked after on_error
        """
        logger.error("on_error is called")

    def on_close(self):
        """
        We need rerun the WebSocket loop in another thread. Because we are
        currently at the end of a WebSocket loop running inside
        self.ws_loop_thread.

        DO NOT join on that thread, that is the current thread
        """
        logger.error("on_close is called")

    async def on_binary(self, binary):
        try:
            while binary:
                packet_length, header_length, _, operation, _ = (ws_struct.unpack_from(binary))
                if operation == WS_OP_MESSAGE:
                    await self.on_message(binary[header_length:packet_length].decode('utf-8', 'ignore'))
                elif operation == WS_OP_CONNECT_SUCCESS:
                    logger.info('直播间 {} 连接成功'.format(self.room_id))
                elif operation == WS_OP_HEARTBEAT_REPLY:
                    logger.debug('Receive room {} heart beat.'.format(self.room_id))
                binary = binary[packet_length:]
        except Exception as e:
            logger.warning("cannot decode message: %s" % e)
            return

    def set_cmd_func(self, cmd, func):
        if not isinstance(func, function):
            raise TypeError('func must be a function')
        self._cmd_func[cmd] = func

    async def on_message(self, message):
        message = (json.loads(message))
        cmd_func = self._cmd_func.get(message['cmd'])
        if cmd_func:
            try:
                await cmd_func(self, message)
            except Exception as e:
                logger.exception('cannot process with func %s, error: %s ' % (cmd_func.__name__, e))
