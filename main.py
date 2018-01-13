# coding: utf-8
__author__ = 'Paul Cunningham'
__copyright = 'Copyright 2017, Paul Cunningham'


try:
    from os import scandir, walk
except ImportError:
    from scandir import scandir, walk
from os.path import basename
import sys
import io
import argparse
import functools
import reusables

if __name__ == '__main__':

    argv = sys.argv[1:]
    sys.argv = sys.argv[:1]
    if "--" in argv:
        index = argv.index("--")
        kivy_args = argv[index + 1:]
        argv = argv[:index]

        sys.argv.extend(kivy_args)

    parser = argparse.ArgumentParser(description="Kivy Slideshow, a simple slideshow that casts to Chromecast.")
    parser.add_argument("-D", "--directory", required=True, help="Slideshow directory, defaults to ~/Pictures")
    parser.add_argument("-P", "--port", type=int, default=8888, required=False, help="Slideshow webserver port, defaults to 9990")
    parser.add_argument("-I", "--interval", type=int, default=5, required=False, help="Slideshow interval in seconds, defaults to 5")
    parser.add_argument('-C', "--cast", help='Enable Chromecast', action='store_true')
    parser.add_argument('--', dest="args", help="Kivy arguments. All arguments after this are interpreted by Kivy. Pass \"-- --help\" to get Kivy's usage.")
    args = parser.parse_args(argv)

import pychromecast
from uritools import uricompose
import mimetypes
import kivy.compat
import kivy
kivy.require('1.0.6')
from kivy import Config
from kivy.resources import resource_find
from kivy.core.image import Image as CoreImage
from kivy.app import App
from kivy.core.window import Window
from kivy.logger import Logger
from kivy.uix.behaviors import CoverBehavior
from kivy.uix.image import Image
from kivy.uix.floatlayout import FloatLayout
from kivy.clock import Clock
from box import Box
from PIL import Image as PillowImage
from helpers import InformationPopup

from kivy.support import install_twisted_reactor
install_twisted_reactor()

from twisted.internet import reactor, endpoints
from twisted.web.server import Site
from twisted.web.static import File


# https://stackoverflow.com/a/30462851/2800058
def image_transpose_exif(im):
    exif_orientation_tag = 0x0112 # contains an integer, 1 through 8
    exif_transpose_sequences = [  # corresponding to the following
        [],
        [PillowImage.FLIP_LEFT_RIGHT],
        [PillowImage.ROTATE_180],
        [PillowImage.FLIP_TOP_BOTTOM],
        [PillowImage.FLIP_LEFT_RIGHT, PillowImage.ROTATE_90],
        [PillowImage.ROTATE_270],
        [PillowImage.FLIP_TOP_BOTTOM, PillowImage.ROTATE_90],
        [PillowImage.ROTATE_90],
    ]

    try:
        seq = exif_transpose_sequences[im._getexif()[exif_orientation_tag] - 1]
    except Exception:
        return im
    else:
        return functools.reduce(lambda im, op: im.transpose(op), seq, im)


class CastController(object):

    def __init__(self, media_controller, host, port):
        self.media_controller = media_controller
        self.scheme = 'http'
        self.host = host
        self.port = port
        mimetypes.init()

    def cast(self, image):
        if self.media_controller:
            _url = uricompose(scheme=self.scheme, host=self.host, port=self.port, path='/' + image)
            _content_type = mimetypes.guess_type(_url, strict=True)
            self.media_controller.play_media(url=_url, content_type=_content_type)


class MainImage(Image):

    def __init__(self, meta=None, cast_controller=None, **kwargs):
        super(MainImage, self).__init__(**kwargs)
        self.cast_controller = cast_controller
        self.meta = meta
        self.source = self.meta.list[0].path

        texture = self._coreimage.texture
        self.reference_size = texture.size
        self.texture = texture

    def texture_update(self, *largs):
        if not self.source:
            self.texture = None
        else:
            filename = resource_find(self.source)
            self._loops = 0
            if filename is None:
                return Logger.error('Image: Error reading file {filename}'.
                                    format(filename=self.source))
            mipmap = self.mipmap
            if self._coreimage is not None:
                self._coreimage.unbind(on_texture=self._on_tex_change)
            try:
                if kivy.compat.PY2 and isinstance(filename, str):
                    filename = filename.decode('utf-8')

                pillow_image = PillowImage.open(filename)
                _format = pillow_image.format.lower()
                pillow_image = image_transpose_exif(pillow_image)
                _bytes = io.BytesIO()
                pillow_image.save(_bytes, format=_format)
                _bytes.seek(0)
                self._coreimage = ci = CoreImage(_bytes, ext=_format, filename=filename, mipmap=mipmap,
                                                 anim_delay=self.anim_delay,
                                                 keep_data=self.keep_data,
                                                 nocache=self.nocache)
            except Exception as ex:
                print ex
                Logger.error('Image: Error loading texture {filename}'.
                                    format(filename=self.source))
                self._coreimage = ci = None

            if ci:
                ci.bind(on_texture=self._on_tex_change)
                self.texture = ci.texture

    def next_image(self, is_loop=False):
        if self.meta.pos < len(self.meta.list) - 1:
            self.meta.pos += 1
            self.source = self.meta.list[self.meta.pos].path
            self._cast()
        else:
            if is_loop:
                self.meta.pos = 1
                self.source = self.meta.list[self.meta.pos].path
                self._cast()

    def previous_image(self):
        if self.meta.pos > 0:
            self.meta.pos -= 1
            self.source = self.meta.list[self.meta.pos].path
            self._cast()

    def _cast(self):
        if self.cast_controller:
            self.cast_controller.cast(basename(self.source))


class ImageViewer(FloatLayout):

    def __init__(self, base_directory, interval, cast_controller, **kwargs):
        super(ImageViewer, self).__init__(**kwargs)
        self.info_popup = InformationPopup()
        self.paused = False
        # Capture keyboard input
        self._keyboard = Window.request_keyboard(self._keyboard_closed, self, 'text')
        if self._keyboard.widget:
            # If it exists, this widget is a VKeyboard object which you can use
            # to change the keyboard layout.
            pass
        self._keyboard.bind(on_key_down=self._on_keyboard_down)

        self.interval = interval

        # Make ImageViewer object take up entire parent space
        self.pos = (0, 0)
        self.size_hint = (1, 1)

        # Meta data about the image set shared between main image and previews
        self.image_data = Box()
        self.image_data.base = base_directory
        self.image_data.pos = 0
        self.image_data.list = []
        self._get_images()

        # Define widgets used so we can reference them elsewhere
        self.image = MainImage(meta=self.image_data, cast_controller=cast_controller)
        # self.next_button = NextButton()
        # self.prev_button = PrevButton()

        self.add_widget(self.image, index=2)
        # self.add_widget(self.preview, index=0)
        # self.add_widget(self.next_button, index=1)
        # self.add_widget(self.prev_button, index=1)

        # call my_callback every 10 seconds
        Clock.schedule_interval(self._schedule_callback, self.interval)

    def _keyboard_closed(self):

        self._keyboard.unbind(on_key_down=self._on_keyboard_down)
        self._keyboard = None

    def _on_keyboard_down(self, keyboard, keycode, text, modifiers):

        if keycode[1] == 'escape':
            if keyboard.window.fullscreen == 'auto':
                keyboard.window.fullscreen = False
            else:
                App.stop(App.get_running_app())

        if keycode[1] == 'f11':
            keyboard.window.toggle_fullscreen()
            keyboard.window.update_viewport()

        if keycode[1] == 'right':
            self.image.next_image()

        if keycode[1] == 'left':
            self.image.previous_image()

        if keycode[1] == 'p':
            self.paused = not self.paused
            self.info_popup.text = '{} slideshow ...'.format('Pausing' if self.paused else 'Starting')
            self.info_popup.open()

        # Return True to accept the key. Otherwise, it will be used by the system.
        return True

    def _get_images(self):

        def _is_acceptable_file(directory_entry):
            return directory_entry.is_file(follow_symlinks=False) and directory_entry.path.lower().endswith(reusables.exts.pictures)

        self.image_data.list = [de for de in sorted(scandir(self.image_data.base)) if _is_acceptable_file(de)]

    def _schedule_callback(self, dt):
        if not self.paused:
            self.image.next_image(is_loop=True)


class MainWindow(FloatLayout):

    def __init__(self, base_directory, interval, cast_controller, **kwargs):
        super(MainWindow, self).__init__(**kwargs)
        self.image_view = ImageViewer(base_directory=base_directory, interval=interval, cast_controller=cast_controller)
        self.add_widget(self.image_view)


class SlideshowApp(App):

    def __init__(self, base_directory, interval, port, host, cast=None, **kwargs):
        super(SlideshowApp, self).__init__(**kwargs)
        self.base_directory = base_directory
        self.interval=interval
        self.port = port
        self.host = host
        self.cast = cast
        self.title = "Slideshow"

    def build(self):
        cast_controller = None
        if self.cast:
            resource = File(self.base_directory)
            factory = Site(resource)
            endpoint = endpoints.TCP4ServerEndpoint(reactor, self.port, interface=self.host)
            endpoint.listen(factory)
            cast_controller = CastController(media_controller=cast.media_controller, host=self.host, port=self.port)
        return MainWindow(base_directory=self.base_directory, interval=self.interval, cast_controller=cast_controller)


if __name__ == '__main__':

    _base_directory = args.directory
    _port = args.port
    _interval = max(1, args.interval)
    _enable_cast = args.cast

    cast = None
    if _enable_cast:
        casts = pychromecast.get_chromecasts()
        cast = casts[0] if len(casts) > 0 else None
    else:
        print "Casting disabled"

    import netifaces
    gws = netifaces.gateways()
    default_gateway_address = gws['default'][netifaces.AF_INET][0]
    default_gateway_interface = gws['default'][netifaces.AF_INET][1]

    for interface in netifaces.interfaces():
        if interface == default_gateway_interface:
            address = netifaces.ifaddresses(interface)
            local_lan_address = address[netifaces.AF_INET][0]['addr']
            break

    SlideshowApp(base_directory=_base_directory, port=_port, interval=_interval, cast=cast, host=local_lan_address).run()
