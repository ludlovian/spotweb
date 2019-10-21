# : vim:foldnestmax=2
"""
music.spotuil

Spotify wrapping utilities

spotutil.start() - to start the spotify infrastructure and login

Player

"""

import yaml, logging, threading, collections, queue, json
from pathlib import Path
from bunch import *

session = None # global session
logger = logging.getLogger(__name__)
Image = None

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
            credentials = bunchify(json.load(f))
    c = credentials
    if 'appkey64' in c and 'appkey' not in c:
        import base64
        c.appkey = base64.b64decode(c.appkey64)

    logger.debug('loading pyspotify')
    import spotify
    if disable_spotify_logging:
        logging.getLogger('spotify').setLevel(logging.WARN)
        #sl = logging.getLogger('spotify')
        #for h in sl.handlers:
        #    sl.removeHandler(h)
        #sl.addHandler(logging.NullHandler())
        #sl.propogate=False
        #sl.setLevel(logging.ERROR)
    logger.debug("pyspotify loaded")

    cfg = spotify.Config()
    cfg.application_key = c.appkey
    cfg.cache_location = c.cachedir
    cfg.settings_location = c.cachedir
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

    session.login(c.username, c.password)
    if not logged_in.wait(60):
        raise LoginError('Failed to login after 60 seconds')
    logger.debug("logged in to spotify")


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
    MUSIC = 'music'
    END_OF_TRACK = 'end_of_track'
    ERROR = 'error'
    BLANK_500MS = b'\0' * (2 * 2 * 44100 // 2) # 2 channels @ 2 bytes
    TIMEOUT=30

    def __init__(self, uri, maxqsize=10):
        self._playing = False
        self.track = session.get_track(uri).load() # track to play
        self._postbox = queue.Queue()
        self._maxqsize = maxqsize # dont let queue grow larger than this
        self._notify = None
        self._notify_period = self._next_notify = 0

    def set_notify(self, callback, freq=60*44100):
        self._notify = callback
        self._notify_period = freq

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

    def on_music(self, session, audio_format, frames, num_frames):
        """Receives the music from spotify

        Posts into the queue, and flags that data is available
        """
        if self._postbox.qsize() > self._maxqsize:
            return 0 # nothing consumed, try again later

        self._postbox.put(Bunch(
            cmd=self.MUSIC,
            frames=frames,
            num_frames=num_frames,
            sample_rate=audio_format.sample_rate,
            channels=audio_format.channels))
        return num_frames # consumed them all

    def on_end_of_track(self, session):
        "Callback when spotify has finished sending data"
        self.stop()
        self._postbox.put(Bunch(
            cmd=self.END_OF_TRACK))

    def on_error(self, session, error_type):
        self.stop()
        self._postbox.put(Bunch(
            cmd=self.ERROR,
            error=repr(error_type)))

    def on_play_token_lost(self, session):
        self.stop()
        self._postbox.put(Bunch(
            cmd=self.ERROR,
            error='Play token lost'))

    def _set_callbacks(self):
        from spotify import SessionEvent as se
        session.on(se.CONNECTION_ERROR, self.on_error)
        session.on(se.STREAMING_ERROR,  self.on_error)
        session.on(se.MUSIC_DELIVERY,   self.on_music)
        session.on(se.PLAY_TOKEN_LOST,  self.on_play_token_lost)
        session.on(se.END_OF_TRACK,     self.on_end_of_track)

    def _clear_callbacks(self):
        from spotify import SessionEvent as se
        session.off(se.CONNECTION_ERROR, self.on_error)
        session.off(se.STREAMING_ERROR,  self.on_error)
        session.off(se.MUSIC_DELIVERY,   self.on_music)
        session.off(se.PLAY_TOKEN_LOST,  self.on_play_token_lost)
        session.off(se.END_OF_TRACK,     self.on_end_of_track)

    def _handle_packet(self, packet):
        """Processes a packet of data, performing notifies if 
        needed, and returning the tuple to be yielded to the client
        """
        self._frame_count += packet.num_frames
        ex = Bunch(
            frame_count=self._frame_count,
            num_frames=packet.num_frames,
            sample_rate=packet.sample_rate,
            channels=packet.channels)

        # call a notify (if set) if due
        while self._notify and self._frame_count >= self._next_notify:
            logger.debug(
                    "Calling notify after %d frames",
                    self._next_notify)
            self._notify(self._next_notify)
            self._next_notify += self._notify_period

        return (packet.frames, ex)

    def get_data_ex(self):
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
        self._frame_count = 0
        self._next_notify = self._notify_period
        cache = collections.deque()
        min_cache_size = 1 # to contain the last packet
                           # but could be set higher if we want

        while True:
            try:
                packet = self._postbox.get(timeout=self.TIMEOUT)
            except queue.Empty:
                raise PlayError('Timed out waiting for data')
            if packet.cmd == self.ERROR:
                raise PlayError(packet.error)
            if packet.cmd == self.END_OF_TRACK:
                logger.debug('End of track received')
                break # Nothing left, so succesful end
            assert packet.cmd == self.MUSIC
            cache.append(packet)
            while len(cache)>min_cache_size:
                yield self._handle_packet(cache.popleft())

        # we have finished receiving, so we drain the cache, removing
        # any padded silence
        while len(cache):
            packet = cache.popleft()
            if packet.frames == self.BLANK_500MS:
                logger.info('Skipping final 500ms of silence')
                pass # do not yield it
            else:
                yield self._handle_packet(packet)

        # and that's it - all finished


    def get_data(self):
        for data, ex in self.get_data_ex():
            yield data


def test():
    logging.basicConfig(level=logging.DEBUG)

    start()
    t1,t2=session.get_album('spotify:album:4qWzxEpG4gax8CGVQUFwqA')\
            .browse().load().tracks[22:24]
    def notify(n):
        m,s = divmod(n//44100,60)
        logger.info("Got %02d:%02d of music",m,s)
    p=Player(str(t1.link))
    p.set_notify(notify,5*44100)
    with Path('test.pcm').open('wb') as f:
        for d in p.get_data():
            f.write(d)
    p=Player(str(t2.link))
    p.set_notify(notify,2*44100)
    with Path('test2.pcm').open('wb') as f:
        for d in p.get_data():
            f.write(d)

if __name__ == '__main__':
    test()
