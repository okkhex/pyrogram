import asyncio
import functools
import inspect
import logging
import os
import platform
import re
import shutil
import sys
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime, timedelta
from hashlib import sha256
from importlib import import_module
from io import StringIO, BytesIO
from mimetypes import MimeTypes
from pathlib import Path
from typing import Union, List, Optional, Callable, AsyncGenerator, Tuple
import pyrogram
from pyrogram import __version__, __license__
from pyrogram import enums
from pyrogram import raw
from pyrogram import types
from pyrogram import utils
from pyrogram.crypto import aes
from pyrogram.errors import CDNFileHashMismatch
from pyrogram.errors import (
    SessionPasswordNeeded,
    VolumeLocNotFound, ChannelPrivate,
    BadRequest, ChannelInvalid, PersistentTimestampInvalid, PersistentTimestampOutdated
)
from pyrogram.handlers.handler import Handler
from pyrogram.methods import Methods
from pyrogram.session import Auth, Session
from pyrogram.storage import FileStorage, MemoryStorage, Storage
from pyrogram.types import User
from pyrogram.utils import ainput
from .connection import Connection
from .connection.transport import TCPAbridged
from .dispatcher import Dispatcher
from .file_id import FileId, FileType, ThumbnailSource
from .mime_types import mime_types
from .parser import Parser
from .session.internals import MsgId

log = logging.getLogger(__name__)
MONGO_AVAIL = False
try:
    import pymongo
except Exception:
    pass
else:
    from pyrogram.storage import MongoStorage
    MONGO_AVAIL = True

class Client(Methods):
    APP_VERSION = f"Pyrogram {__version__}"
    DEVICE_MODEL = f"{platform.python_implementation()} {platform.python_version()}"
    SYSTEM_VERSION = f"{platform.system()} {platform.release()}"
    LANG_CODE = "en"
    PARENT_DIR = Path(sys.argv[0]).parent
    INVITE_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:org|me|dog)/(?:joinchat/|\+))([\w-]+)$")
    UPGRADED_GIFT_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:org|me|dog)/(?:nft/|\+))([\w-]+)$")
    WORKERS = min(32, (os.cpu_count() or 0) + 4)  # os.cpu_count() can be None
    WORKDIR = PARENT_DIR
    # Interval of seconds in which the updates watchdog will kick in
    UPDATES_WATCHDOG_INTERVAL = 15 * 60
    MAX_CONCURRENT_TRANSMISSIONS = 1
    MAX_CACHE_SIZE = 10000
    mimetypes = MimeTypes()
    mimetypes.readfp(StringIO(mime_types))

    def __init__(
        self,
        name: str,
        api_id: Optional[Union[int, str]] = None,
        api_hash: Optional[str] = None,
        app_version: str = APP_VERSION,
        device_model: str = DEVICE_MODEL,
        system_version: str = SYSTEM_VERSION,
        lang_code: str = LANG_CODE,
        ipv6: Optional[bool] = False,
        alt_port: Optional[bool] = False,
        proxy: Optional[dict] = None,
        test_mode: Optional[bool] = False,
        bot_token: Optional[str] = None,
        session_string: Optional[str] = None,
        use_qrcode: Optional[bool] = False,
        in_memory: Optional[bool] = None,
        mongodb: Optional[dict] = None,
        storage: Optional[Storage] = None,
        phone_number: Optional[str] = None,
        phone_code: Optional[str] = None,
        password: Optional[str] = None,
        workers: int = WORKERS,
        workdir: Union[str, Path] = WORKDIR,
        parse_mode: "enums.ParseMode" = enums.ParseMode.DEFAULT,
        no_updates: Optional[bool] = None,
        skip_updates: bool = True,
        takeout: bool = None,
        sleep_threshold: int = Session.SLEEP_THRESHOLD,
        hide_password: Optional[bool] = True,
        max_concurrent_transmissions: int = MAX_CONCURRENT_TRANSMISSIONS,
        client_platform: "enums.ClientPlatform" = enums.ClientPlatform.OTHER,
        max_message_cache_size: int = MAX_CACHE_SIZE,
        max_business_user_connection_cache_size: int = MAX_CACHE_SIZE
    ):
        super().__init__()

        self.name = name
        self.api_id = int(api_id) if api_id else None
        self.api_hash = api_hash
        self.app_version = app_version
        self.device_model = device_model
        self.system_version = system_version
        self.lang_code = lang_code.lower()
        self.ipv6 = ipv6
        self.alt_port = alt_port
        self.proxy = proxy
        self.test_mode = test_mode
        self.bot_token = bot_token
        self.session_string = session_string
        self.use_qrcode = use_qrcode
        self.in_memory = in_memory
        self.mongodb = mongodb
        self.phone_number = phone_number
        self.phone_code = phone_code
        self.password = password
        self.workers = workers
        self.workdir = Path(workdir)
        self.parse_mode = parse_mode
        self.no_updates = no_updates
        self.skip_updates = skip_updates
        self.takeout = takeout
        self.sleep_threshold = sleep_threshold
        self.hide_password = hide_password
        self.max_concurrent_transmissions = max_concurrent_transmissions
        self.client_platform = client_platform
        self.max_message_cache_size = max_message_cache_size
        self.max_message_cache_size = max_message_cache_size
        self.max_business_user_connection_cache_size = max_business_user_connection_cache_size

        self.executor = ThreadPoolExecutor(self.workers, thread_name_prefix="Handler")

        if storage:
            self.storage = storage
        elif self.session_string:
            self.storage = MemoryStorage(self.name, self.session_string)
        elif self.in_memory:
            self.storage = MemoryStorage(self.name)
        elif self.mongodb:
            if not MONGO_AVAIL:
                log.warning(
                    "pymongo is missing! "
                    "Using MemoryStorage as session storage"
                )
                self.storage = MemoryStorage(self.name)
            else:
                self.storage = MongoStorage(self.name, **self.mongodb)
        else:
            self.storage = FileStorage(self.name, self.workdir)

        self.connection_factory = Connection
        self.protocol_factory = TCPAbridged

        self.dispatcher = Dispatcher(self)

        self.rnd_id = MsgId

        self.parser = Parser(self)

        self.session = None

        self.media_sessions = {}
        self.media_sessions_lock = asyncio.Lock()

        self.save_file_semaphore = asyncio.Semaphore(self.max_concurrent_transmissions)
        self.get_file_semaphore = asyncio.Semaphore(self.max_concurrent_transmissions)

        self.is_connected = None
        self.is_initialized = None

        self.takeout_id = None

        self.disconnect_handler = None

        self.me: Optional[User] = None

        self.message_cache = Cache(self.max_message_cache_size)
        self.business_user_connection_cache = Cache(self.max_business_user_connection_cache_size)

      
        self.updates_watchdog_task = None
        self.updates_watchdog_event = asyncio.Event()
        self.last_update_time = datetime.now()
        self.listeners = {listener_type: [] for listener_type in pyrogram.enums.ListenerTypes}
        self.loop = asyncio.get_event_loop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        try:
            self.stop()
        except ConnectionError:
            pass

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *args):
        try:
            await self.stop()
        except ConnectionError:
            pass

    async def updates_watchdog(self):
        while True:
            try:
                await asyncio.wait_for(self.updates_watchdog_event.wait(), self.UPDATES_WATCHDOG_INTERVAL)
            except asyncio.TimeoutError:
                pass
            else:
                break

            if datetime.now() - self.last_update_time > timedelta(seconds=self.UPDATES_WATCHDOG_INTERVAL):
                await self.invoke(raw.functions.updates.GetState())

    async def _wait_for_update_login_token(self):
        """
        Wait for an UpdateLoginToken update from Telegram.
        """
        while True:
            update, _, _ = await self.dispatcher.updates_queue.get()
            if isinstance(update, raw.types.UpdateLoginToken):
                break

    async def authorize(self) -> User:
        if self.bot_token:
            return await self.sign_in_bot(self.bot_token)

       
        if not self.use_qrcode:
            while True:
                try:   
                    if not self.phone_number:
                        while True:
                            print("Enter 'qrcode' if you want to login with qrcode.")
                            value = await ainput("Enter phone number or bot token: ")

                            if not value:
                                continue

                            if value.lower() == "qrcode":
                                self.use_qrcode = True
                                break

                            confirm = (await ainput(f'Is "{value}" correct? (y/N): ')).lower()

                            if confirm == "y":
                                break

                        if ":" in value:
                            self.bot_token = value
                            return await self.sign_in_bot(value)
                        else:
                            self.phone_number = value

                    sent_code = await self.send_code(self.phone_number)
                except BadRequest as e:
                    print(e.MESSAGE)
                    self.phone_number = None
                    self.bot_token = None
                else:
                    break

            sent_code_descriptions = {
                enums.SentCodeType.APP: "Telegram app",
                enums.SentCodeType.SMS: "SMS",
                enums.SentCodeType.CALL: "phone call",
                enums.SentCodeType.FLASH_CALL: "phone flash call",
                enums.SentCodeType.FRAGMENT_SMS: "Fragment SMS",
                enums.SentCodeType.EMAIL_CODE: "email code"
            }

            print(f"The confirmation code has been sent via {sent_code_descriptions[sent_code.type]}")

        while True:
            if not self.use_qrcode and not self.phone_code:
                self.phone_code = await ainput("Enter confirmation code: ")

            try:
                if self.use_qrcode:
                    signed_in = await self.sign_in_qrcode()
                else:
                    signed_in = await self.sign_in(self.phone_number, sent_code.phone_code_hash, self.phone_code)
            except BadRequest as e:
                print(e.MESSAGE)
                self.phone_code = None
            except SessionPasswordNeeded as e:
                print(e.MESSAGE)

                while True:
                    print("Password hint: {}".format(await self.get_password_hint()))

                    if not self.password:
                        self.password = await ainput("Enter password (empty to recover): ", hide=self.hide_password)

                    try:
                        if not self.password:
                            confirm = await ainput("Confirm password recovery (y/n): ")

                            if confirm == "y":
                                email_pattern = await self.send_recovery_code()
                                print(f"The recovery code has been sent to {email_pattern}")

                                while True:
                                    recovery_code = await ainput("Enter recovery code: ")

                                    try:
                                        return await self.recover_password(recovery_code)
                                    except BadRequest as e:
                                        print(e.MESSAGE)
                                    except Exception as e:
                                        log.exception(e)
                                        raise
                            else:
                                self.password = None
                        else:
                            return await self.check_password(self.password)
                    except BadRequest as e:
                        print(e.MESSAGE)
                        self.password = None
            else:
                if self.use_qrcode and isinstance(signed_in, types.LoginToken):
                    time_out = signed_in.expires - datetime.timestamp(datetime.now())
                    try:
                        await asyncio.wait_for(self._wait_for_update_login_token(), timeout=time_out)
                    except asyncio.TimeoutError:
                        print("QR code expired, Requesting new Qr code...")
                    continue
                break

        if isinstance(signed_in, User):
            return signed_in

    def set_parse_mode(self, parse_mode: Optional["enums.ParseMode"]):
 

        self.parse_mode = parse_mode

    async def fetch_peers(self, peers: List[Union[raw.types.User, raw.types.Chat, raw.types.Channel]]) -> bool:
        is_min = False
        parsed_peers = []
        usernames = []

        for peer in peers:
            if getattr(peer, "min", False):
                is_min = True
                continue

            username = None
            phone_number = None

            if isinstance(peer, raw.types.User):
                peer_id = peer.id
                access_hash = peer.access_hash
                username = (
                    peer.username.lower() if peer.username
                    else peer.usernames[0].username.lower() if peer.usernames
                    else None
                )
                if peer.usernames is not None and len(peer.usernames) > 1:
                    for uname in peer.usernames:
                        usernames.append((peer_id, uname.username.lower()))
                phone_number = peer.phone
                peer_type = "bot" if peer.bot else "user"
            elif isinstance(peer, (raw.types.Chat, raw.types.ChatForbidden)):
                peer_id = -peer.id
                access_hash = 0
                peer_type = "group"
            elif isinstance(peer, raw.types.Channel):
                peer_id = utils.get_channel_id(peer.id)
                access_hash = peer.access_hash
                username = (
                    peer.username.lower() if peer.username
                    else peer.usernames[0].username.lower() if peer.usernames
                    else None
                )
                if peer.usernames is not None and len(peer.usernames) > 1:
                    for uname in peer.usernames:
                        usernames.append((peer_id, uname.username.lower()))
                peer_type = "channel" if peer.broadcast else "supergroup"
            elif isinstance(peer, raw.types.ChannelForbidden):
                peer_id = utils.get_channel_id(peer.id)
                access_hash = peer.access_hash
                peer_type = "channel" if peer.broadcast else "supergroup"
            else:
                continue

            parsed_peers.append((peer_id, access_hash, peer_type, username, phone_number))

        await self.storage.update_peers(parsed_peers)
        await self.storage.update_usernames(usernames)

        return is_min

    async def handle_updates(self, updates):
        self.last_update_time = datetime.now()

        if isinstance(updates, (raw.types.Updates, raw.types.UpdatesCombined)):
            is_min = any((
                await self.fetch_peers(updates.users),
                await self.fetch_peers(updates.chats),
            ))

            users = {u.id: u for u in updates.users}
            chats = {c.id: c for c in updates.chats}

            for update in updates.updates:
                channel_id = getattr(
                    getattr(
                        getattr(
                            update, "message", None
                        ), "peer_id", None
                    ), "channel_id", None
                ) or getattr(update, "channel_id", None)

                pts = getattr(update, "pts", None)
                pts_count = getattr(update, "pts_count", None)

                if pts:
                    await self.storage.update_state(
                        (
                            utils.get_channel_id(channel_id) if channel_id else 0,
                            pts,
                            None,
                            updates.date,
                            None
                        )
                    )

                if isinstance(update, raw.types.UpdateChannelTooLong):
                    log.info(update)

                if isinstance(update, raw.types.UpdateNewChannelMessage) and is_min:
                    message = update.message

                    if not isinstance(message, raw.types.MessageEmpty):
                        try:
                            diff = await self.invoke(
                                raw.functions.updates.GetChannelDifference(
                                    channel=await self.resolve_peer(utils.get_channel_id(channel_id)),
                                    filter=raw.types.ChannelMessagesFilter(
                                        ranges=[raw.types.MessageRange(
                                            min_id=update.message.id,
                                            max_id=update.message.id
                                        )]
                                    ),
                                    pts=pts - pts_count,
                                    limit=pts,
                                    force=False
                                )
                            )
                        except (ChannelPrivate, PersistentTimestampOutdated, PersistentTimestampInvalid):
                            pass
                        else:
                            if not isinstance(diff, raw.types.updates.ChannelDifferenceEmpty):
                                users.update({u.id: u for u in diff.users})
                                chats.update({c.id: c for c in diff.chats})

                self.dispatcher.updates_queue.put_nowait((update, users, chats))
        elif isinstance(updates, (raw.types.UpdateShortMessage, raw.types.UpdateShortChatMessage)):
            await self.storage.update_state(
                (
                    0,
                    updates.pts,
                    None,
                    updates.date,
                    None
                )
            )

            diff = await self.invoke(
                raw.functions.updates.GetDifference(
                    pts=updates.pts - updates.pts_count,
                    date=updates.date,
                    qts=-1
                )
            )

            if diff.new_messages:
                self.dispatcher.updates_queue.put_nowait((
                    raw.types.UpdateNewMessage(
                        message=diff.new_messages[0],
                        pts=updates.pts,
                        pts_count=updates.pts_count
                    ),
                    {u.id: u for u in diff.users},
                    {c.id: c for c in diff.chats}
                ))
            else:
                if diff.other_updates:  # The other_updates list can be empty
                    self.dispatcher.updates_queue.put_nowait((diff.other_updates[0], {}, {}))
        elif isinstance(updates, raw.types.UpdateShort):
            self.dispatcher.updates_queue.put_nowait((updates.update, {}, {}))
        elif isinstance(updates, raw.types.UpdatesTooLong):
            log.info(updates)

    async def recover_gaps(self) -> Tuple[int, int]:
        states = await self.storage.update_state()

        message_updates_counter = 0
        other_updates_counter = 0

        if not states:
            log.info("No states found, skipping recovery.")
            return (message_updates_counter, other_updates_counter)

        for state in states:
            id, local_pts, _, local_date, _ = state

            prev_pts = 0

            while True:
                try:
                    diff = await self.invoke(
                        raw.functions.updates.GetChannelDifference(
                            channel=await self.resolve_peer(id),
                            filter=raw.types.ChannelMessagesFilterEmpty(),
                            pts=local_pts,
                            limit=10000,
                            force=False
                        ) if id < 0 else
                        raw.functions.updates.GetDifference(
                            pts=local_pts,
                            date=local_date,
                            qts=0
                        )
                    )
                except (ChannelPrivate, ChannelInvalid, PersistentTimestampOutdated, PersistentTimestampInvalid):
                    break

                if isinstance(diff, raw.types.updates.DifferenceEmpty):
                    break
                elif isinstance(diff, raw.types.updates.DifferenceTooLong):
                    break
                elif isinstance(diff, raw.types.updates.Difference):
                    local_pts = diff.state.pts
                elif isinstance(diff, raw.types.updates.DifferenceSlice):
                    local_pts = diff.intermediate_state.pts
                    local_date = diff.intermediate_state.date

                    if prev_pts == local_pts:
                        break

                    prev_pts = local_pts
                elif isinstance(diff, raw.types.updates.ChannelDifferenceEmpty):
                    break
                elif isinstance(diff, raw.types.updates.ChannelDifferenceTooLong):
                    break
                elif isinstance(diff, raw.types.updates.ChannelDifference):
                    local_pts = diff.pts

                users = {i.id: i for i in diff.users}
                chats = {i.id: i for i in diff.chats}

                for message in diff.new_messages:
                    message_updates_counter += 1
                    self.dispatcher.updates_queue.put_nowait(
                        (
                            raw.types.UpdateNewMessage(
                                message=message,
                                pts=local_pts,
                                pts_count=-1
                            ),
                            users,
                            chats
                        )
                    )

                for update in diff.other_updates:
                    other_updates_counter += 1
                    self.dispatcher.updates_queue.put_nowait(
                        (update, users, chats)
                    )

                if isinstance(diff, (raw.types.updates.Difference, raw.types.updates.ChannelDifference)):
                    break

            await self.storage.update_state(id)

        log.info("Recovered %s messages and %s updates.", message_updates_counter, other_updates_counter)
        return (message_updates_counter, other_updates_counter)

    async def handle_download(self, packet):
        file_id, directory, file_name, in_memory, file_size, progress, progress_args = packet

        _ = os.makedirs(directory, exist_ok=True) if not in_memory else None
        temp_file_path = os.path.abspath(re.sub("\\\\", "/", os.path.join(directory, file_name))) + ".temp"
        file = BytesIO() if in_memory else open(temp_file_path, "wb")

        try:
            async for chunk in self.get_file(file_id, file_size, 0, 0, progress, progress_args):
                file.write(chunk)
        except BaseException as e:
            if not in_memory:
                file.close()
                os.remove(temp_file_path)

            if isinstance(e, asyncio.CancelledError):
                raise e

            if isinstance(e, pyrogram.errors.FloodWait):
                raise e

            return None
        else:
            if in_memory:
                file.name = file_name
                return file
            else:
                file.close()
                file_path = os.path.splitext(temp_file_path)[0]
                shutil.move(temp_file_path, file_path)
                return file_path

    async def get_file(
        self,
        file_id: FileId,
        file_size: int = 0,
        limit: int = 0,
        offset: int = 0,
        progress: Callable = None,
        progress_args: tuple = ()
    ) -> Optional[AsyncGenerator[bytes, None]]:
        async with self.get_file_semaphore:
            file_type = file_id.file_type

            if file_type == FileType.CHAT_PHOTO:
                if file_id.chat_id > 0:
                    peer = raw.types.InputPeerUser(
                        user_id=file_id.chat_id,
                        access_hash=file_id.chat_access_hash
                    )
                else:
                    if file_id.chat_access_hash == 0:
                        peer = raw.types.InputPeerChat(
                            chat_id=-file_id.chat_id
                        )
                    else:
                        peer = raw.types.InputPeerChannel(
                            channel_id=utils.get_channel_id(file_id.chat_id),
                            access_hash=file_id.chat_access_hash
                        )

                location = raw.types.InputPeerPhotoFileLocation(
                    peer=peer,
                    photo_id=file_id.media_id,
                    big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG
                )
            elif file_type == FileType.PHOTO:
                location = raw.types.InputPhotoFileLocation(
                    id=file_id.media_id,
                    access_hash=file_id.access_hash,
                    file_reference=file_id.file_reference,
                    thumb_size=file_id.thumbnail_size
                )
            else:
                location = raw.types.InputDocumentFileLocation(
                    id=file_id.media_id,
                    access_hash=file_id.access_hash,
                    file_reference=file_id.file_reference,
                    thumb_size=file_id.thumbnail_size
                )

            current = 0
            total = abs(limit) or (1 << 31) - 1
            chunk_size = 1024 * 1024
            offset_bytes = abs(offset) * chunk_size

            dc_id = file_id.dc_id

            session = Session(
                self, dc_id,
                await Auth(self, dc_id, await self.storage.test_mode()).create()
                if dc_id != await self.storage.dc_id()
                else await self.storage.auth_key(),
                await self.storage.test_mode(),
                is_media=True
            )

            try:
                await session.start()

                if dc_id != await self.storage.dc_id():
                    exported_auth = await self.invoke(
                        raw.functions.auth.ExportAuthorization(
                            dc_id=dc_id
                        )
                    )

                    await session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id,
                            bytes=exported_auth.bytes
                        )
                    )

                r = await session.invoke(
                    raw.functions.upload.GetFile(
                        location=location,
                        offset=offset_bytes,
                        limit=chunk_size
                    ),
                    sleep_threshold=30
                )

                if isinstance(r, raw.types.upload.File):
                    while True:
                        chunk = r.bytes

                        yield chunk

                        current += 1
                        offset_bytes += chunk_size

                        if progress:
                            func = functools.partial(
                                progress,
                                min(offset_bytes, file_size)
                                if file_size != 0
                                else offset_bytes,
                                file_size,
                                *progress_args
                            )

                            if inspect.iscoroutinefunction(progress):
                                await func()
                            else:
                                await self.loop.run_in_executor(self.executor, func)

                        if len(chunk) < chunk_size or current >= total:
                            break

                        r = await session.invoke(
                            raw.functions.upload.GetFile(
                                location=location,
                                offset=offset_bytes,
                                limit=chunk_size
                            ),
                            sleep_threshold=30
                        )

                elif isinstance(r, raw.types.upload.FileCdnRedirect):
                    cdn_session = Session(
                        self, r.dc_id, await Auth(self, r.dc_id, await self.storage.test_mode()).create(),
                        await self.storage.test_mode(), is_media=True, is_cdn=True
                    )

                    try:
                        await cdn_session.start()

                        while True:
                            r2 = await cdn_session.invoke(
                                raw.functions.upload.GetCdnFile(
                                    file_token=r.file_token,
                                    offset=offset_bytes,
                                    limit=chunk_size
                                )
                            )

                            if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
                                try:
                                    await session.invoke(
                                        raw.functions.upload.ReuploadCdnFile(
                                            file_token=r.file_token,
                                            request_token=r2.request_token
                                        )
                                    )
                                except VolumeLocNotFound:
                                    break
                                else:
                                    continue

                            chunk = r2.bytes

                            # https://core.telegram.org/cdn#decrypting-files
                            decrypted_chunk = aes.ctr256_decrypt(
                                chunk,
                                r.encryption_key,
                                bytearray(
                                    r.encryption_iv[:-4]
                                    + (offset_bytes // 16).to_bytes(4, "big")
                                )
                            )

                            hashes = await session.invoke(
                                raw.functions.upload.GetCdnFileHashes(
                                    file_token=r.file_token,
                                    offset=offset_bytes
                                )
                            )

                            # https://core.telegram.org/cdn#verifying-files
                            for i, h in enumerate(hashes):
                                cdn_chunk = decrypted_chunk[h.limit * i: h.limit * (i + 1)]
                                CDNFileHashMismatch.check(
                                    h.hash == sha256(cdn_chunk).digest(),
                                    "h.hash == sha256(cdn_chunk).digest()"
                                )

                            yield decrypted_chunk

                            current += 1
                            offset_bytes += chunk_size

                            if progress:
                                func = functools.partial(
                                    progress,
                                    min(offset_bytes, file_size) if file_size != 0 else offset_bytes,
                                    file_size,
                                    *progress_args
                                )

                                if inspect.iscoroutinefunction(progress):
                                    await func()
                                else:
                                    await self.loop.run_in_executor(self.executor, func)

                            if len(chunk) < chunk_size or current >= total:
                                break
                    except Exception as e:
                        raise e
                    finally:
                        await cdn_session.stop()
            except pyrogram.StopTransmission:
                raise
            except pyrogram.errors.FloodWait:
                raise
            except Exception as e:
                log.exception(e)
            finally:
                await session.stop()

    def guess_mime_type(self, filename: str) -> Optional[str]:
        return self.mimetypes.guess_type(filename)[0]

    def guess_extension(self, mime_type: str) -> Optional[str]:
        return self.mimetypes.guess_extension(mime_type)


class Cache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.store = {}

    def __getitem__(self, key):
        return self.store.get(key, None)

    def __setitem__(self, key, value):
        if key in self.store:
            del self.store[key]

        self.store[key] = value

        if len(self.store) > self.capacity:
            for _ in range(self.capacity // 2 + 1):
                del self.store[next(iter(self.store))]
