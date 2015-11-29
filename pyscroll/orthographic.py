import logging
import math
import time
from functools import partial
from heapq import heappush, heappop
from itertools import product, chain
from operator import gt
import pygame
from pygame import Surface, Rect
from pyscroll import surface_clipping_context, quadtree
from pyscroll.animation import AnimationFrame, AnimationToken

logger = logging.getLogger('orthographic')


class BufferedRenderer(object):
    """ Renderer that support scrolling, zooming, layers, and animated tiles

    The buffered renderer must be used with a data class to get tile, shape,
    and animation information.  See the data class api in pyscroll.data, or
    use the built-in pytmx support for loading maps created with Tiled.
    """
    def __init__(self, data, size, clamp_camera=True, colorkey=None, alpha=False,
                 time_source=time.time, scaling_function=pygame.transform.scale):

        # default options
        self.map_rect = None                       # pygame rect of entire map
        self.data = data                           # reference to data source
        self.clamp_camera = clamp_camera           # if clamped, cannot scroll past map edge
        self.time_source = time_source             # determines how tile animations are processed
        self.scaling_function = scaling_function   # what function to use when zooming
        self.default_shape_texture_gid = 1         # [experimental] texture to draw shapes with
        self.default_shape_color = 0, 255, 0       # [experimental] color to fill polygons with

        # internal private defaults
        self._alpha = False
        if colorkey and alpha:
            print('cannot select both colorkey and alpha.  choose one.')
            raise ValueError
        elif colorkey:
            self._clear_color = colorkey
        else:
            self._clear_color = None

        # private attributes
        self._size = None             # size that the camera/viewport is on screen, kinda
        self._redraw_cutoff = None    # size of dirty tile edge that will trigger full redraw
        self._x_offset = None         # offsets are used to scroll map in sub-tile increments
        self._y_offset = None
        self._buffer = None           # complete rendering of tilemap
        self._tile_view = None        # this rect represents each tile on the buffer
        self._half_width = None       # 'half x' attributes are used to reduce division ops.
        self._half_height = None
        self._tile_queue = None       # tiles queued to be draw onto buffer
        self._animation_queue = None  # heap queue of animation token.  schedules tile changes
        self._animation_map = None    # map of GID to other GIDs in an animation
        self._last_time = None        # used for scheduling animations
        self._layer_quadtree = None   # used to draw tiles that overlap optional surfaces
        self._zoom_buffer = None      # used to speed up zoom operations
        self._zoom_level = 1.0        # negative numbers make map smaller, positive: bigger

        # this represents the viewable pixels, aka 'camera'
        self.view_rect = Rect(0, 0, 0, 0)

        self.reload_animations()
        self.set_size(size)

    def _update_time(self):
        self._last_time = time.time() * 1000

    def reload_animations(self):
        """ Reload animation information
        """
        self._update_time()
        self._animation_map = dict()
        self._animation_queue = list()

        for gid, frame_data in self.data.get_animations():
            frames = list()
            for frame_gid, frame_duration in frame_data:
                image = self.data.get_tile_image_by_gid(frame_gid)
                frames.append(AnimationFrame(image, frame_duration))

            ani = AnimationToken(gid, frames)
            ani.next += self._last_time
            self._animation_map[ani.gid] = ani.frames[ani.index].image
            heappush(self._animation_queue, ani)

    def _process_animation_queue(self):
        self._update_time()
        requires_redraw = False

        # test if the next scheduled tile change is ready
        while self._animation_queue[0].next <= self._last_time:
            requires_redraw = True
            token = heappop(self._animation_queue)

            # advance the animation index, looping by default
            if token.index == len(token.frames) - 1:
                token.index = 0
            else:
                token.index += 1

            next_frame = token.frames[token.index]
            token.next = next_frame.duration + self._last_time
            self._animation_map[token.gid] = next_frame.image
            heappush(self._animation_queue, token)

        if requires_redraw:
            # TODO: record the tiles that changed and update only affected tiles
            self.redraw_tiles()
            pass

    def _calculate_zoom_buffer_size(self, value):
        if value <= 0:
            print('zoom level cannot be zero or less')
            raise ValueError
        value = 1.0 / value
        return [int(round(i * value)) for i in self._size]

    @property
    def zoom(self):
        """ Zoom the map in or out.

        Increase this number to make map appear to come closer to camera.
        Decrease this number to make map appear to move away from camera.

        Default value is 1.0
        This value cannot be negative or 0.0

        :return: float
        """
        return self._zoom_level

    @zoom.setter
    def zoom(self, value):
        self._zoom_level = value
        buffer_size = self._calculate_zoom_buffer_size(value)
        self._initialize_buffers(buffer_size)

    def set_size(self, size):
        """ Set the size of the map in pixels

        This is an expensive operation, do only when absolutely needed.

        :param size: (width, height) pixel size of camera/view of the group
        """
        self._size = size
        buffer_size = self._calculate_zoom_buffer_size(self._zoom_level)
        self._initialize_buffers(buffer_size)

    def _create_buffers(self, view_size, buffer_size):
        """ Create the buffers, taking in account pixel alpha or colorkey

        :param view_size: pixel size of the view
        :param buffer_size: pixel size of the buffer
        """
        requires_zoom_buffer = not view_size == buffer_size
        self._zoom_buffer = None

        if self._clear_color:
            if requires_zoom_buffer:
                self._zoom_buffer = Surface(view_size, flags=pygame.RLEACCEL)
                self._zoom_buffer.set_colorkey(self._clear_color)
            self._buffer = Surface(buffer_size, flags=pygame.RLEACCEL)
            self._buffer.set_colorkey(self._clear_color)
            self._buffer.fill(self._clear_color)
        elif self._alpha:
            if requires_zoom_buffer:
                self._zoom_buffer = Surface(view_size, flags=pygame.SRCALPHA)
            self._buffer = Surface(buffer_size, flags=pygame.SRCALPHA)
        else:
            if requires_zoom_buffer:
                self._zoom_buffer = Surface(view_size)
            self._buffer = Surface(buffer_size)

    def _initialize_buffers(self, size):
        """ Create the buffers to cache tile drawing

        :param size: (int, int): size of the draw area
        :return: None
        """
        tw, th = self.data.tile_size
        mw, mh = self.data.map_size
        buffer_tile_width = int(math.ceil(size[0] / tw) + 2)
        buffer_tile_height = int(math.ceil(size[1] / th) + 2)
        buffer_pixel_size = buffer_tile_width * tw, buffer_tile_height * th

        self.map_rect = Rect(0, 0, mw * tw, mh * th)
        self.view_rect.size = size
        self._tile_view = Rect(0, 0, buffer_tile_width, buffer_tile_height)
        self._redraw_cutoff = min(buffer_tile_width, buffer_tile_height)
        self._create_buffers(size, buffer_pixel_size)
        self._half_width = size[0] // 2
        self._half_height = size[1] // 2
        self._x_offset = 0
        self._y_offset = 0

        def make_rect(x, y):
            return Rect((x * tw, y * th), (tw, th))

        rects = [make_rect(*i) for i in product(range(buffer_tile_width),
                                                range(buffer_tile_height))]

        # TODO: figure out what depth -actually- does
        self._layer_quadtree = quadtree.FastQuadTree(rects, 4)

        self.redraw_tiles()

    def scroll(self, vector):
        """ scroll the background in pixels

        :param vector: (int, int)
        """
        self.center((vector[0] + self.view_rect.centerx,
                     vector[1] + self.view_rect.centery))

    def center(self, coords):
        """ center the map on a pixel

        float numbers will be rounded.

        :param coords: (number, number)
        """
        x, y = [round(i, 0) for i in coords]
        self.view_rect.center = x, y

        if self.clamp_camera:
            self.view_rect.clamp_ip(self.map_rect)
            x, y = self.view_rect.center

        # calc the new position in tiles and offset
        tw, th = self.data.tile_size
        left, self._x_offset = divmod(x - self._half_width, tw)
        top, self._y_offset = divmod(y - self._half_height, th)

        # adjust the view if the view has changed without a redraw
        dx = int(left - self._tile_view.left)
        dy = int(top - self._tile_view.top)
        view_change = max(abs(dx), abs(dy))

        if view_change <= self._redraw_cutoff:
            self._buffer.scroll(-dx * tw, -dy * th)
            self._tile_view.move_ip((dx, dy))
            self._queue_edge_tiles(dx, dy)
            self._flush_tile_queue()

        elif view_change > self._redraw_cutoff:
            logger.info('scrolling too quickly.  redraw forced')
            self._tile_view.move_ip((dx, dy))
            self.redraw_tiles()

    def _queue_edge_tiles(self, dx, dy):
        """ Queue edge tiles and clear edge areas on buffer if needed

        :param dx: Edge along X axis to enqueue
        :param dy: Edge along Y axis to enqueue
        :return: None
        """
        v = self._tile_view
        fill = partial(self._buffer.fill, self._clear_color)
        tw, th = self.data.tile_size
        self._tile_queue = iter([])

        def append(rect):
            self._tile_queue = chain(self._tile_queue, self.data.get_tile_images_by_rect(rect))
            if self._clear_color:
                fill(((rect[0] - self._tile_view.left) * tw,
                      (rect[1] - self._tile_view.top) * th,
                      rect[2] * tw, rect[3] * th))

        if dx > 0:    # right side
            append((v.right - dx, v.top, dx, v.height))

        elif dx < 0:  # left side
            append((v.left, v.top, -dx, v.height))

        if dy > 0:    # bottom side
            append((v.left, v.bottom - dy, v.width, dy))

        elif dy < 0:  # top side
            append((v.left, v.top, v.width, -dy))

    def draw(self, surface, rect, surfaces=None):
        """ Draw the map onto a surface

        pass a rect that defines the draw area for:
            drawing to an area smaller that the whole window/screen

        surfaces may optionally be passed that will be blitted onto the surface.
        this must be a list of tuples containing a layer number, image, and
        rect in screen coordinates.  surfaces will be drawn in order passed,
        and will be correctly drawn with tiles from a higher layer overlapping
        the surface.

        surfaces list should be in the following format:
        [ (layer, surface, rect), ... ]

        :param surface: pygame surface to draw to
        :param rect: area to draw to
        :param surfaces: optional sequence of surfaces to interlace into tiles
        """
        if self._zoom_level == 1.0:
            self._render_map(surface, rect, surfaces)
        else:
            self._render_map(self._zoom_buffer, self._zoom_buffer.get_rect(), surfaces)
            self.scaling_function(self._zoom_buffer, rect.size, surface)

    def _render_map(self, surface, rect, surfaces):
        """ Render the map and optional surfaces to destination surface

        :param surface: pygame surface to draw to
        :param rect: area to draw to
        :param surfaces: optional sequence of surfaces to interlace into tiles
        """
        if self._animation_queue:
            self._process_animation_queue()

        if rect.width > self.map_rect.width:
            x = (rect.width - self.map_rect.width) // 4
            print(x)
            self._x_offset += x

        if rect.height > self.map_rect.height:
            pass

        # need to set clipping otherwise the map will draw outside its area
        with surface_clipping_context(surface, rect):
            # draw the entire map to the surface taking in account the scrolling offset
            surface.blit(self._buffer, (-self._x_offset - rect.left,
                                        -self._y_offset - rect.top))
            if surfaces:
                self._draw_surfaces(surface, rect, surfaces)

    def _draw_surfaces(self, surface, rect, surfaces):
        """ Draw surfaces onto map, then redraw tiles that cover them

        :param surface: destination
        :param rect: clipping area
        :param surfaces: sequence of surfaces to blit
        """
        surface_blit = surface.blit
        left, top = self._tile_view.topleft
        ox = self._x_offset - rect.left
        oy = self._y_offset - rect.top
        hit = self._layer_quadtree.hit
        get_tile = self.data.get_tile_image
        tile_layers = tuple(self.data.visible_tile_layers)
        dirty = [(surface_blit(i[0], i[1]), i[2]) for i in surfaces]

        for dirty_rect, layer in dirty:
            for r in hit(dirty_rect.move(ox, oy)):
                x, y, tw, th = r
                for l in [i for i in tile_layers if gt(i, layer)]:
                    tile = get_tile((x // tw + left, y // th + top, l))
                    if tile:
                        surface_blit(tile, (x - ox, y - oy))

    def _draw_objects(self):
        """ Totally unoptimized drawing of objects to the map [probably broken]
        """
        tw, th = self.data.tile_size
        buff = self._buffer
        blit = buff.blit
        map_gid = self.data.tmx.map_gid
        default_color = self.default_shape_color
        get_image_by_gid = self.data.get_tile_image_by_gid
        _draw_textured_poly = pygame.gfxdraw.textured_polygon
        _draw_poly = pygame.draw.polygon
        _draw_lines = pygame.draw.lines

        ox = self._tile_view.left * tw
        oy = self._tile_view.top * th

        def draw_textured_poly(texture, points):
            try:
                _draw_textured_poly(buff, points, texture, tw, th)
            except pygame.error:
                pass

        def draw_poly(color, points, width=0):
            _draw_poly(buff, color, points, width)

        def draw_lines(color, points, width=2):
            _draw_lines(buff, color, False, points, width)

        def to_buffer(pt):
            return pt[0] - ox, pt[1] - oy

        for layer in self.data.visible_object_layers:
            for o in (o for o in layer if o.visible):
                texture_gid = getattr(o, "texture", None)
                color = getattr(o, "color", default_color)

                # BUG: this is not going to be completely accurate, because it
                # does not take into account times where texture is flipped.
                if texture_gid:
                    texture_gid = map_gid(texture_gid)[0][0]
                    texture = get_image_by_gid(int(texture_gid))

                if hasattr(o, 'points'):
                    points = [to_buffer(i) for i in o.points]
                    if o.closed:
                        if texture_gid:
                            draw_textured_poly(texture, points)
                        else:
                            draw_poly(color, points)
                    else:
                        draw_lines(color, points)

                elif o.gid:
                    tile = get_image_by_gid(o.gid)
                    if tile:
                        pt = to_buffer((o.x, o.y))
                        blit(tile, pt)

                else:
                    x, y = to_buffer((o.x, o.y))
                    points = ((x, y), (x + o.width, y),
                              (x + o.width, y + o.height), (x, y + o.height))
                    if texture_gid:
                        draw_textured_poly(texture, points)
                    else:
                        draw_poly(color, points)

    def _flush_tile_queue(self):
        """ Blit the queued tiles and block until the tile queue is empty
        """
        tw, th = self.data.tile_size
        ltw = self._tile_view.left * tw
        tth = self._tile_view.top * th
        blit = self._buffer.blit
        map_get = self._animation_map.get

        for x, y, l, tile, gid in self._tile_queue:
            blit(map_get(gid, tile), (x * tw - ltw, y * th - tth))

    def redraw_tiles(self):
        """ redraw the visible portion of the buffer -- it is slow.
        """
        if self._clear_color:
            self._buffer.fill(self._clear_color)
        elif self._alpha:
            self._buffer.fill(0)

        self._tile_queue = self.data.get_tile_images_by_rect(self._tile_view)
        self._flush_tile_queue()

    def get_center_offset(self):
        """ Return x, y pair that will change world coords to screen coords
        :return: int, int
        """
        return (-self.view_rect.centerx + self._half_width,
                -self.view_rect.centery + self._half_height)
