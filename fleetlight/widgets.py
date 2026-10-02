"""Cairo-drawn dashboard widgets. Colours come from libadwaita's named palette so they follow the theme.

Callbacks are plain functions that receive the widget, never bound methods or closures over it: a
bound method stored in a GTK closure keeps its widget alive forever, these widgets are rebuilt on every
refresh, and PyGObject may hand a callback a fresh wrapper for a widget nothing in Python still holds.
"""
import math
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Gtk, PangoCairo

TONES = {"accent": ("accent_color", (0.47, 0.68, 0.93)), "good": ("success_color", (0.56, 0.94, 0.64)),
         "warning": ("warning_color", (0.97, 0.83, 0.36)), "bad": ("error_color", (1.0, 0.48, 0.39))}
ROUND_CAP = 1
GLIDE_MS = 450


def tone_colour(widget, tone):
    name, fallback = TONES.get(tone or "accent", TONES["accent"])
    found, colour = widget.get_style_context().lookup_color(name)
    return (colour.red, colour.green, colour.blue) if found else fallback


def clamp(value):
    return max(0.0, min(1.0, float(value))) if isinstance(value, (int, float)) else 0.0


def draw_text(widget, context, text, x, y, size, weight=400, alpha=1.0, align=0.0, valign=0.0, colour=None):
    """Draw text at (x, y); align and valign shift it by that share of its own size."""
    layout = widget.create_pango_layout(None)
    layout.set_markup('<span size="%d" weight="%d" font_features="tnum">%s</span>'
                      % (round(size * 1024), weight, GLib.markup_escape_text(str(text))), -1)
    width, height = layout.get_pixel_size()
    if colour is None:
        foreground = widget.get_color()
        colour = (foreground.red, foreground.green, foreground.blue)
    context.set_source_rgba(*colour, alpha)
    context.move_to(x - width * align, y - height * valign)
    PangoCairo.show_layout(context, layout)
    return width, height


def rounded(context, x, y, width, height, radius):
    radius = min(radius, width / 2, height / 2)
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()


class Ring(Gtk.DrawingArea):
    """Circular gauge with a caption in the middle. Value changes glide instead of jumping."""

    def __init__(self, value=None, text="", tone="accent", size=56, thickness=6, start=None, text_size=None):
        super().__init__(content_width=size, content_height=size, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.thickness = thickness
        self.text = text
        self.tone = tone
        self.text_size = text_size or max(8.0, size * 0.19)
        self.target = clamp(value)
        self.shown = self.target if start is None else clamp(start)
        self.origin = self.shown
        self.started = None
        self.ticking = False
        self.set_draw_func(Ring.draw)
        if abs(self.shown - self.target) > 0.004:
            self.connect("map", Ring.glide)

    def get_text(self):
        return self.text

    def set_value(self, value, text=None, tone=False, animate=True):
        """Show a new reading. Frequent live updates pass animate=False: a glide redraws the window
        at the display's refresh rate, which adds up when it happens every two seconds."""
        target = clamp(value)
        text = self.text if text is None else text
        tone = self.tone if tone is False else tone
        if (target, text, tone) == (self.target, self.text, self.tone) and not self.ticking:
            return
        self.target, self.text, self.tone = target, text, tone
        if animate and self.get_mapped():
            self.glide()
        else:
            self.shown = self.target
            self.queue_draw()

    def glide(self):
        if abs(self.shown - self.target) <= 0.004 or not self.get_settings().get_property("gtk-enable-animations"):
            self.shown = self.target
            self.queue_draw()
            return
        self.origin = self.shown
        self.started = None
        if not self.ticking:
            self.ticking = True
            self.add_tick_callback(Ring.tick)

    def tick(self, clock):
        """Ease towards the target over GLIDE_MS; GTK passes the widget, so nothing is captured."""
        now = clock.get_frame_time()
        if self.started is None:
            self.started = now
        progress = min(1.0, (now - self.started) / (GLIDE_MS * 1000))
        self.shown = self.origin + (self.target - self.origin) * (1 - (1 - progress) ** 3)
        self.queue_draw()
        if progress >= 1:
            self.ticking = False
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def draw(self, context, width, height):
        colour = tone_colour(self, self.tone)
        radius = min(width, height) / 2 - self.thickness / 2
        context.set_line_width(self.thickness)
        context.set_line_cap(ROUND_CAP)
        foreground = self.get_color()
        context.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.11)
        context.arc(width / 2, height / 2, radius, 0, 2 * math.pi)
        context.stroke()
        if self.shown > 0.004:
            context.set_source_rgba(*colour, 1)
            context.arc(width / 2, height / 2, radius, -math.pi / 2, -math.pi / 2 + 2 * math.pi * self.shown)
            context.stroke()
        if self.text:
            draw_text(self, context, self.text, width / 2, height / 2, self.text_size, 700, 0.95, 0.5, 0.5)


class FleetRing(Gtk.DrawingArea):
    """Donut split by state: one arc per group of computers, with a headline in the middle."""

    def __init__(self, segments, title, caption, size=104, thickness=11):
        super().__init__(content_width=size, content_height=size, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.segments = [(count, tone) for count, tone in segments if count > 0]
        self.title = title
        self.caption = caption
        self.thickness = thickness
        self.set_draw_func(FleetRing.draw)

    def draw(self, context, width, height):
        radius = min(width, height) / 2 - self.thickness / 2
        centre = (width / 2, height / 2)
        foreground = self.get_color()
        context.set_line_width(self.thickness)
        context.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.11)
        context.arc(*centre, radius, 0, 2 * math.pi)
        context.stroke()
        total = sum(count for count, _ in self.segments)
        if total:
            context.set_line_cap(ROUND_CAP)
            # Round caps extend each arc by half the stroke, so leave that much room plus a visible gap.
            gap = (self.thickness + 4) / radius if len(self.segments) > 1 else 0
            angle = -math.pi / 2 + gap / 2
            for count, tone in self.segments:
                sweep = 2 * math.pi * count / total
                colour = tone_colour(self, tone) if tone else (foreground.red, foreground.green, foreground.blue)
                context.set_source_rgba(*colour, 1 if tone else 0.3)
                if len(self.segments) == 1:
                    context.arc(*centre, radius, 0, 2 * math.pi)
                else:
                    context.arc(*centre, radius, angle, angle + max(0.02, sweep - gap))
                context.stroke()
                angle += sweep
        draw_text(self, context, self.title, centre[0], centre[1] + 3, height * 0.17, 800, 1, 0.5, 0.78)
        draw_text(self, context, self.caption, centre[0], centre[1] + 4, height * 0.085, 600, 0.62, 0.5, 0)


class MiniBars(Gtk.DrawingArea):
    """Three thin meters for CPU, memory and disk, used where a full gauge does not fit."""

    def __init__(self, values, tones):
        super().__init__(content_width=30, content_height=20, valign=Gtk.Align.CENTER)
        self.values = values
        self.tones = tones
        self.set_draw_func(MiniBars.draw)

    def update(self, values, tones):
        if (values, tones) != (self.values, self.tones):
            self.values, self.tones = values, tones
            self.queue_draw()

    def draw(self, context, width, height):
        foreground = self.get_color()
        count = len(self.values)
        thickness = 3.0
        gap = (height - thickness * count) / max(1, count - 1) if count > 1 else 0
        for index, value in enumerate(self.values):
            y = index * (thickness + gap)
            context.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.13)
            rounded(context, 0, y, width, thickness, thickness / 2)
            context.fill()
            if isinstance(value, (int, float)) and value > 0:
                colour = tone_colour(self, self.tones[index]) if self.tones[index] else (foreground.red, foreground.green, foreground.blue)
                context.set_source_rgba(*colour, 1 if self.tones[index] else 0.62)
                rounded(context, 0, y, max(thickness, width * clamp(value / 100)), thickness, thickness / 2)
                context.fill()


class Sparkline(Gtk.DrawingArea):
    """Small filled trend line for a 0–100 metric."""

    def __init__(self, values, tone="accent", height=30):
        super().__init__(hexpand=True, content_height=height)
        self.values = [max(0, min(100, float(v))) for v in values if isinstance(v, (int, float))]
        self.tone = tone
        self.set_draw_func(Sparkline.draw)

    def draw(self, context, width, height):
        points = self.values
        if len(points) < 2 or width < 8:
            return
        colour = tone_colour(self, self.tone)
        top, bottom = 3.0, height - 2.0
        step = (width - 2.0) / (len(points) - 1)
        coords = [(1.0 + index * step, bottom - (bottom - top) * value / 100) for index, value in enumerate(points)]
        context.move_to(coords[0][0], bottom)
        for x, y in coords:
            context.line_to(x, y)
        context.line_to(coords[-1][0], bottom)
        context.close_path()
        context.set_source_rgba(*colour, 0.16)
        context.fill()
        context.move_to(*coords[0])
        for x, y in coords[1:]:
            context.line_to(x, y)
        context.set_source_rgba(*colour, 0.9)
        context.set_line_width(1.6)
        context.stroke()


def nice_ceiling(value):
    """Round a maximum up to a tidy axis limit."""
    if value <= 0:
        return 1
    magnitude = 10 ** math.floor(math.log10(value))
    for factor in (1, 2, 2.5, 5, 10):
        if value <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def time_label(moment, span):
    return time.strftime("%H:%M:%S" if span <= 900 else "%H:%M" if span <= 36 * 3600 else "%a %H:%M", time.localtime(moment))


class TrendChart(Gtk.DrawingArea):
    """Area chart over time with gaps where nothing was measured and a readout under the pointer."""
    LEFT, RIGHT, TOP, BOTTOM = 4, 44, 10, 22

    def __init__(self, height=170):
        super().__init__(hexpand=True, content_height=height)
        self.points = []
        self.start = self.end = 0.0
        self.unit = "%"
        self.ceiling = 100.0
        self.tone = "accent"
        self.empty_text = "Collecting history…"
        self.pointer = None
        self.set_draw_func(TrendChart.draw)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", TrendChart.moved)
        motion.connect("leave", TrendChart.left)
        self.add_controller(motion)

    @staticmethod
    def moved(controller, x, _y):
        chart = controller.get_widget()
        chart.pointer = x
        chart.queue_draw()

    @staticmethod
    def left(controller):
        chart = controller.get_widget()
        chart.pointer = None
        chart.queue_draw()

    def set_data(self, points, start, end, unit="%", ceiling=100, tone="accent", empty_text=None):
        """points are (time, value) pairs; ceiling None scales the axis to the data."""
        self.points = [(moment, value) for moment, value in points
                       if isinstance(value, (int, float)) and start <= moment <= end]
        self.start, self.end, self.unit, self.tone = float(start), float(end), unit, tone
        if ceiling is None:
            ceiling = nice_ceiling(max((value for _, value in self.points), default=1) * 1.15)
        self.ceiling = float(ceiling)
        if empty_text:
            self.empty_text = empty_text
        self.queue_draw()

    def runs(self):
        """Split the series wherever checks are missing, so outages show as gaps, not straight lines."""
        if len(self.points) < 2:
            return [self.points] if self.points else []
        steps = sorted(b[0] - a[0] for a, b in zip(self.points, self.points[1:]))
        limit = max(3.5 * steps[len(steps) // 2], (self.end - self.start) / 60, 10)
        result, current = [], [self.points[0]]
        for previous, point in zip(self.points, self.points[1:]):
            if point[0] - previous[0] > limit:
                result.append(current)
                current = []
            current.append(point)
        result.append(current)
        return result

    def format(self, value):
        if self.unit == "%":
            return f"{value:.0f}%"
        if self.unit == "°C":
            return f"{value:.0f} °C"
        if self.unit == "ms":
            return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms"
        return f"{value:g} {self.unit}".strip()

    def draw(self, context, width, height):
        foreground = self.get_color()
        ink = (foreground.red, foreground.green, foreground.blue)
        colour = tone_colour(self, self.tone)
        left, right = self.LEFT, width - self.RIGHT
        top, bottom = self.TOP, height - self.BOTTOM
        if right - left < 40 or bottom - top < 30:
            return
        span = max(1.0, self.end - self.start)

        def place(moment, value):
            return (left + (right - left) * (moment - self.start) / span,
                    bottom - (bottom - top) * max(0.0, min(1.0, value / self.ceiling)))

        context.set_line_width(1)
        for share in (0, 0.5, 1):
            y = round(bottom - (bottom - top) * share) + 0.5
            context.set_source_rgba(*ink, 0.09)
            context.move_to(left, y)
            context.line_to(right, y)
            context.stroke()
            draw_text(self, context, self.format(self.ceiling * share), width - 2, y, 7.6, 500, 0.52, 1, 0.5)
        for share, align in ((0, 0), (0.5, 0.5), (1, 1)):
            draw_text(self, context, time_label(self.start + span * share, span) if share < 1 or self.end < time.time() - 90 else "now",
                      left + (right - left) * share, bottom + 5, 7.6, 500, 0.52, align, 0)
        if not self.points:
            draw_text(self, context, self.empty_text, (left + right) / 2, (top + bottom) / 2, 9.5, 500, 0.5, 0.5, 0.5)
            return
        for run in self.runs():
            coords = [place(moment, value) for moment, value in run]
            if len(coords) == 1:
                context.set_source_rgba(*colour, 0.9)
                context.arc(*coords[0], 2, 0, 2 * math.pi)
                context.fill()
                continue
            smooth = len(coords) < (right - left) / 5

            def trace():
                context.move_to(*coords[0])
                for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                    if smooth:
                        middle = (x0 + x1) / 2
                        context.curve_to(middle, y0, middle, y1, x1, y1)
                    else:
                        context.line_to(x1, y1)
            trace()
            context.line_to(coords[-1][0], bottom)
            context.line_to(coords[0][0], bottom)
            context.close_path()
            context.save()
            context.clip()
            steps = 24
            for index in range(steps):
                # A stepped fade keeps pycairo's pattern classes out of the import list.
                y0 = top + (bottom - top) * index / steps
                context.set_source_rgba(*colour, 0.30 * (1 - index / steps) ** 1.5 + 0.02)
                context.rectangle(left, y0, right - left, (bottom - top) / steps + 0.6)
                context.fill()
            context.restore()
            trace()
            context.set_source_rgba(*colour, 1)
            context.set_line_width(1.8)
            context.set_line_join(1)
            context.stroke()
        last = place(*self.points[-1])
        if self.pointer is None:
            if self.end - self.points[-1][0] < span / 30 + 120:
                context.set_source_rgba(*colour, 0.25)
                context.arc(*last, 6, 0, 2 * math.pi)
                context.fill()
                context.set_source_rgba(*colour, 1)
                context.arc(*last, 3, 0, 2 * math.pi)
                context.fill()
            return
        moment, value = min(self.points, key=lambda point: abs(place(*point)[0] - self.pointer))
        x, y = place(moment, value)
        if abs(x - self.pointer) > 40:
            return
        context.set_source_rgba(*ink, 0.28)
        context.set_line_width(1)
        context.move_to(round(x) + 0.5, top)
        context.line_to(round(x) + 0.5, bottom)
        context.stroke()
        context.set_source_rgba(*colour, 0.25)
        context.arc(x, y, 7, 0, 2 * math.pi)
        context.fill()
        context.set_source_rgba(*colour, 1)
        context.arc(x, y, 3.5, 0, 2 * math.pi)
        context.fill()
        text = self.format(value) + "  ·  " + time_label(moment, span)
        layout = self.create_pango_layout(text)
        text_width = layout.get_pixel_size()[0] * 0.92 + 18
        box_x = max(left, min(right - text_width, x - text_width / 2))
        box_y = top if y > top + 40 else bottom - 26
        dark = sum(ink) > 1.5
        context.set_source_rgba(*((0.1, 0.1, 0.12) if dark else (1, 1, 1)), 0.94)
        rounded(context, box_x, box_y, text_width, 22, 7)
        context.fill()
        context.set_source_rgba(*ink, 0.16)
        rounded(context, box_x + 0.5, box_y + 0.5, text_width - 1, 21, 7)
        context.set_line_width(1)
        context.stroke()
        draw_text(self, context, text, box_x + text_width / 2, box_y + 11, 8.4, 600, 0.95, 0.5, 0.5)


class AvailabilityStrip(Gtk.DrawingArea):
    """Status-page style row of blocks: green when every check answered, red when none did."""

    def __init__(self, segments, start, end, height=18):
        super().__init__(hexpand=True, content_height=height, has_tooltip=True)
        self.segments = list(segments)
        self.start, self.end = start, end
        self.set_draw_func(AvailabilityStrip.draw)
        self.connect("query-tooltip", AvailabilityStrip.describe)

    def describe(self, x, _y, _keyboard, tooltip):
        if not self.segments or self.get_width() <= 0:
            return False
        index = max(0, min(len(self.segments) - 1, int(x / self.get_width() * len(self.segments))))
        width = (self.end - self.start) / len(self.segments)
        begin = self.start + index * width
        value = self.segments[index]
        state = ("No checks" if value is None else "Reachable" if value >= 0.999
                 else "Unreachable" if value <= 0.001 else f"Reachable {round(100 * value)}% of the time")
        tooltip.set_text(time.strftime("%a %H:%M", time.localtime(begin)) + "–" +
                         time.strftime("%H:%M", time.localtime(begin + width)) + " · " + state)
        return True

    def draw(self, context, width, height):
        count = len(self.segments)
        if not count:
            return
        foreground = self.get_color()
        gap = 2.0 if width / count >= 5 else 1.0
        block = (width - gap * (count - 1)) / count
        for index, value in enumerate(self.segments):
            if value is None:
                context.set_source_rgba(foreground.red, foreground.green, foreground.blue, 0.10)
            else:
                tone = "good" if value >= 0.999 else "bad" if value <= 0.001 else "warning"
                context.set_source_rgba(*tone_colour(self, tone), 0.92)
            rounded(context, index * (block + gap), 0, block, height, min(3, block / 2))
            context.fill()
