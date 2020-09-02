# : vim:foldnestmax=2
"""
music.spotuil

Spotify wrapping utilities

spotutil.start() - to start the spotify infrastructure and login

Player

"""

import logging
import threading
import collections
import queue
import json
from pathlib import Path

BYTES_PER_SECOND = 2 * 2 * 44100
BLANK_500MS = b'\0' * (BYTES_PER_SECOND // 2)
TIMEOUT = 120

session = None # global session
logger = logging.getLogger(__name__)

class _Error(Exception):
    def __init__(self, message):
        self.message = message
    def __repr__(self):
        return "<{}: {!r}>".format(type(self).__name__, self.message)
    __str__ = __repr__

class LoginError(_Error): pass
class PlayError(_Error): pass

def start(credentials=None,
          disable_spotify_logging=True):

    if credentials is None:
        with Path(__file__).with_name('credentials.json').open() as f:
            credentials = json.load(f)
    c = credentials
    if 'appkey64' in c and 'appkey' not in c:
        import base64
        c['appkey'] = base64.b64decode(c['appkey64'])

    logger.debug('loading pyspotify')
    import spotify
    if disable_spotify_logging:
        logging.getLogger('spotify').setLevel(logging.WARN)
    logger.debug("pyspotify loaded")

    cfg = spotify.Config()
    cfg.application_key = c['appkey']
    cfg.cache_location = c['cachedir']
    cfg.settings_location = c['cachedir']
    global session
    session = spotify.Session(cfg)
    session.preferred_bitrate(spotify.Bitrate.BITRATE_320k)

    logged_in = threading.Event()
    def connection_state_changed(session):
        if session.connection.state is spotify.ConnectionState.LOGGED_IN:
            logged_in.set()

    loop = spotify.EventLoop(session)
    loop.start()
    session.on(spotify.SessionEvent.CONNECTION_STATE_UPDATED,
               connection_state_changed)

    session.login(c['username'], c['password'])
    if not logged_in.wait(60):
        raise LoginError('Failed to login after 60 seconds')
    logger.debug("logged in to spotify")

class Packet: pass

class EndPacket(Packet): pass

class ErrorPacket(Packet):
    def __init__(self, error):
        self.error = error

class MusicPacket(Packet):
    def __init__(self, audio_format, frames, num_frames):
        self.audio_format = audio_format
        self.frames = frames
        self.num_frames = num_frames

class Player:
    """
    Spotify track player

    Construct it, and the get the data by using
        for data in player.get_data():
            <do something with the data>

        or

        for data, extra in player.get_data_ex():
            <do something with the data>

    It will throw exceptions on any errors, and will truncate the
    last 22050 samples if they are all zero

    """

    def __init__(self, uri, maxqsize=10):
        self._playing = False
        self.track = session.get_track(uri).load() # track to play
        self._postbox = queue.Queue()
        self._maxqsize = maxqsize # dont let queue grow larger than this

    def _start(self):
        self._set_callbacks()
        self._playing = True
        session.player.load(self.track)
        session.player.play(True)

    def stop(self):
        if not self._playing: return
        session.player.play(False)
        session.player.unload()
        self._clear_callbacks()
        self._playing = False

    def _set_callbacks(self):
        from spotify import SessionEvent as se
        session.on(se.CONNECTION_ERROR, self.on_error)
        session.on(se.STREAMING_ERROR, self.on_error)
        session.on(se.MUSIC_DELIVERY, self.on_music)
        session.on(se.PLAY_TOKEN_LOST, self.on_play_token_lost)
        session.on(se.END_OF_TRACK, self.on_end_of_track)

    def _clear_callbacks(self):
        from spotify import SessionEvent as se
        session.off(se.CONNECTION_ERROR, self.on_error)
        session.off(se.STREAMING_ERROR, self.on_error)
        session.off(se.MUSIC_DELIVERY, self.on_music)
        session.off(se.PLAY_TOKEN_LOST, self.on_play_token_lost)
        session.off(se.END_OF_TRACK, self.on_end_of_track)

    def on_end_of_track(self, session):
        "Callback when spotify has finished sending data"
        self.stop()
        self._postbox.put(EndPacket())

    def on_error(self, session, error_type):
        self.stop()
        self._postbox.put(ErrorPacket(repr(error_type)))

    def on_play_token_lost(self, session):
        self.stop()
        self._postbox.put(ErrorPacket('Play token lost'))

    def on_music(self, session, audio_format, frames, num_frames):
        """Receives the music from spotify

        Posts into the queue, and flags that data is available
        """
        if self._postbox.qsize() > self._maxqsize:
            return 0 # nothing consumed, try again later

        self._postbox.put(MusicPacket(audio_format, frames, num_frames))
        return num_frames # consumed them all

    def get_data(self, end_after=None):
        """
        The meat of the capture

        We fill a deque of data, and yield it out. This enables the
        yielding to run one (or more if needed) packets beind the
        receiving. Once we have fininished, we can examine the last
        packet(s) for the dreaded 500ms of silence that libspotify
        seems to add
        """
        logger.debug('Starting playback')
        self._start()
        byte_count = 0
        cache = collections.deque()
        min_cache_size = 1 # to contain the last packet
                           # but could be set higher if we want

        while True:
            try:
                packet = self._postbox.get(timeout=TIMEOUT)
            except queue.Empty:
                raise PlayError('Timed out waiting for data')
            if isinstance(packet, ErrorPacket):
                raise PlayError(packet.error)
            if isinstance(packet, EndPacket):
                break
            assert isinstance(packet, MusicPacket)
            byte_count += len(packet.frames)
            if end_after and byte_count > end_after * BYTES_PER_SECOND:
                logger.debug('Stopping early as requested')
                self.stop()
                return
            cache.append(packet)
            while len(cache) > min_cache_size:
                packet = cache.popleft()
                yield packet.frames

        # we have finished receiving, so we drain the cache, removing
        # any padded silence
        while len(cache):
            packet = cache.popleft()
            if packet.frames == BLANK_500MS:
                logger.info('Skipping final 500ms of silence')
                pass # do not yield it
            else:
                yield packet.frames

        # and that's it - all finished
        logger.debug('All packets processed')


def test():
    logging.basicConfig(level=logging.DEBUG)

    start()
    t1, t2 = session.get_album('spotify:album:4qWzxEpG4gax8CGVQUFwqA')\
            .browse().load().tracks[22:24]
    p = Player(str(t1.link))
    with Path('test.pcm').open('wb') as f:
        for d in p.get_data(10):
            f.write(d)
    p = Player(str(t2.link))
    with Path('test2.pcm').open('wb') as f:
        for d in p.get_data(10):
            f.write(d)

if __name__ == '__main__':
    test()
