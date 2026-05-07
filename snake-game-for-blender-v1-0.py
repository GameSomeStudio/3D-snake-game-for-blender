# SPDX-FileCopyrightText: 2026 GameSome - mabaci
# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Snake Game 3D",
    "author": "GameSome - mabaci",
    "version": (1, 5, 2),
    "blender": (3, 6, 0),
    "location": "View3D > N Panel > Snake",
    "description": "Classic Snake arcade in 3D Viewport. Take a break while rendering!",
    "category": "Game",
}

import bpy
import blf
import math
import random
import time
import struct
import io
import os
import re

# ============================================================
# CONFIGURATION
# ============================================================

GAME_SPEED = 0.10
GRID_SIZE = 8
TAIL_START = 3
TAIL_SPACING = 0.9

# ============================================================
# ADDON PREFERENCES
# ============================================================

class SnakePreferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    
    player_name: bpy.props.StringProperty(
        name="Player Name", default="Player", maxlen=20
    )
    view_angle: bpy.props.FloatProperty(
        name="Camera Angle", default=30.0, min=10.0, max=85.0
    )
    sound_enabled: bpy.props.BoolProperty(
        name="Sound Effects", default=True
    )
    scene_obstacles: bpy.props.BoolProperty(
        name="Scene Objects as Obstacles", default=False
    )
    graphics_quality: bpy.props.EnumProperty(
        name="Graphics",
        items=[
            ('HIGH', "HIGH", "Bevel + Smooth shading"),
            ('LOW', "LOW", "Flat shading, no bevel (faster)"),
        ],
        default='HIGH',
    )
    
    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="🐍 Snake Game v15.2", icon='SETTINGS')
        box.prop(self, "player_name")
        box.separator()
        box.prop(self, "view_angle", slider=True)
        box.prop(self, "graphics_quality")
        box.prop(self, "sound_enabled")
        box.prop(self, "scene_obstacles")

# ============================================================
# SOUND SYSTEM
# ============================================================

def _wav(freqs, durs, wave='sine'):
    sr = 44100
    samples = []
    for f, d in zip(freqs, durs):
        n = int(sr * d)
        for i in range(n):
            t = i / sr
            v = math.sin(2*math.pi*f*t) if wave=='sine' else 2*abs(2*(t*f-math.floor(t*f+0.5)))-1
            v *= (1.0 - i/n) * 0.3
            samples.append(int(v * 32767))
    buf = io.BytesIO()
    ds = len(samples)*2
    buf.write(b'RIFF'); buf.write(struct.pack('<I', 36+ds)); buf.write(b'WAVEfmt ')
    buf.write(struct.pack('<IHHIIHH', 16, 1, 1, sr, sr*2, 2, 16))
    buf.write(b'data'); buf.write(struct.pack('<I', ds))
    for s in samples: buf.write(struct.pack('<h', s))
    buf.seek(0); return buf

def _play(buf):
    try:
        import aud, tempfile, threading
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
            tmp.write(buf.read()); tmp_path = tmp.name
        aud.Device().play(aud.Sound(tmp_path))
        threading.Timer(2.0, lambda: os.path.exists(tmp_path) and os.remove(tmp_path)).start()
    except: pass

def sfx_eat():
    try: _play(_wav([800, 1200], [0.05, 0.08]))
    except: pass

def sfx_die():
    try: _play(_wav([400, 350, 300, 200], [0.15]*4, 'triangle'))
    except: pass

# ============================================================
# GAME STATE
# ============================================================

class _G:
    active = False; over = False; paused = False; started = False
    score = 0; direction = (1,0); next_dir = (1,0)
    parts = []; positions = []; obstacles = []
    food = ""; walls = []; last_update = 0.0; handle = None; orig = {}
    game_z = 10.0

G = _G()

# ============================================================
# HELPERS
# ============================================================

def _prefs(): return bpy.context.preferences.addons[__name__].preferences

def _mat(name, color, em=0.3):
    if name in bpy.data.materials: bpy.data.materials.remove(bpy.data.materials[name])
    m = bpy.data.materials.new(name); m.use_nodes = True; m.node_tree.nodes.clear()
    e = m.node_tree.nodes.new('ShaderNodeEmission'); e.inputs['Color'].default_value = (*color, 1.0); e.inputs['Strength'].default_value = em
    o = m.node_tree.nodes.new('ShaderNodeOutputMaterial'); m.node_tree.links.new(e.outputs['Emission'], o.inputs['Surface'])
    return m

def _create_materials():
    _mat("HeadMat", (0.1, 0.9, 0.2), 0.5)
    _mat("TailMat", (0.05, 0.6, 0.15), 0.25)
    _mat("FoodMat", (1.0, 0.85, 0.0), 2.5)
    _mat("WallMat", (0.0, 0.7, 1.0), 0.8)
    _mat("FloorMat", (0.03, 0.03, 0.10), 0.08)
    _mat("WallRed", (1.0, 0.1, 0.1), 2.0)

def _add_bevel_shading(obj_name):
    if obj_name not in bpy.data.objects: return
    obj = bpy.data.objects[obj_name]
    if _prefs().graphics_quality == 'HIGH':
        if hasattr(obj.data, 'polygons'):
            for p in obj.data.polygons: p.use_smooth = True
        if obj.name.startswith(("Head", "T", "Wall_", "WN", "WS", "WE", "WW")):
            m = obj.modifiers.new(name="Bevel", type='BEVEL'); m.width = 0.04; m.segments = 1; m.limit_method = 'ANGLE'; m.angle_limit = 1.0
        if obj.name.startswith("Food"):
            m = obj.modifiers.new(name="Subdiv", type='SUBSURF'); m.levels = 1; m.render_levels = 1
    else:
        if hasattr(obj.data, 'polygons'):
            for p in obj.data.polygons: p.use_smooth = False

def _obj(prim, name, loc, mat, **kw):
    if name in bpy.data.objects: bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)
    if prim == 'M': bpy.ops.mesh.primitive_monkey_add(size=kw.get('s',0.4), location=loc)
    elif prim == 'C': bpy.ops.mesh.primitive_cube_add(size=kw.get('s',0.35), location=loc)
    elif prim == 'Y': bpy.ops.mesh.primitive_cylinder_add(radius=kw.get('r',0.3), depth=kw.get('d',0.08), location=loc)
    elif prim == 'P': bpy.ops.mesh.primitive_plane_add(size=kw.get('s',1), location=loc)
    obj = bpy.context.active_object; obj.name = name
    if mat in bpy.data.materials: obj.data.materials.append(bpy.data.materials[mat])
    if 'sc' in kw: obj.scale = kw['sc']
    if 'rt' in kw: obj.rotation_euler = kw['rt']
    _add_bevel_shading(name)
    return obj.name

def _col():
    n = "SnakeCol"
    if n in bpy.data.collections:
        for o in list(bpy.data.collections[n].objects): bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.collections.remove(bpy.data.collections[n])
    c = bpy.data.collections.new(n); bpy.context.scene.collection.children.link(c)
    return c

def _link(obj_or_name):
    obj = bpy.data.objects.get(obj_or_name) if isinstance(obj_or_name, str) else obj_or_name
    if not obj: return
    for c in obj.users_collection: c.objects.unlink(obj)
    if "SnakeCol" in bpy.data.collections: bpy.data.collections["SnakeCol"].objects.link(obj)

def _lock(ctx):
    p = _prefs(); s = ctx.space_data
    G.orig = {'sh': s.shading.type, 'ov': s.overlay.show_overlays, 'gz': s.show_gizmo, 'color': s.shading.color_type, 'light': s.shading.light, 'bg': s.shading.background_type, 'bf': s.shading.show_backface_culling, 'cur': s.overlay.show_cursor, 'flr': s.overlay.show_floor, 'ax': s.overlay.show_axis_x, 'ay': s.overlay.show_axis_y, 'az': s.overlay.show_axis_z, 'grid': s.overlay.show_ortho_grid}
    if s.region_3d:
        G.orig['rt'] = s.region_3d.view_rotation.copy(); G.orig['ds'] = s.region_3d.view_distance; G.orig['loc'] = s.region_3d.view_location.copy(); G.orig['persp'] = s.region_3d.view_perspective; G.orig['lock'] = s.region_3d.lock_rotation
        import mathutils
        s.region_3d.view_rotation = mathutils.Euler((math.radians(p.view_angle), 0, 0), 'XYZ').to_quaternion(); s.region_3d.view_distance = GRID_SIZE*3.0; s.region_3d.view_location = mathutils.Vector((0,0,G.game_z)); s.region_3d.view_perspective = 'PERSP'; s.region_3d.lock_rotation = True
    s.overlay.show_overlays = False; s.show_gizmo = False; s.shading.type = 'SOLID'; s.shading.color_type = 'RANDOM'
    s.shading.light = 'FLAT' if p.graphics_quality == 'LOW' else 'STUDIO'
    s.shading.show_backface_culling = (p.graphics_quality == 'LOW'); s.shading.background_type = 'THEME'

def _unlock(ctx):
    try:
        s = ctx.space_data
        if s.region_3d:
            s.region_3d.lock_rotation = G.orig.get('lock', False)
            if 'rt' in G.orig: s.region_3d.view_rotation = G.orig['rt']; s.region_3d.view_distance = G.orig.get('ds',10); s.region_3d.view_location = G.orig.get('loc',(0,0,0)); s.region_3d.view_perspective = G.orig.get('persp','PERSP')
        s.shading.type = G.orig.get('sh','SOLID'); s.shading.color_type = G.orig.get('color','MATERIAL'); s.shading.light = G.orig.get('light','STUDIO'); s.shading.background_type = G.orig.get('bg','THEME'); s.shading.show_backface_culling = G.orig.get('bf',True)
        s.overlay.show_overlays = G.orig.get('ov',True); s.overlay.show_cursor = G.orig.get('cur',True); s.overlay.show_floor = G.orig.get('flr',True); s.overlay.show_axis_x = G.orig.get('ax',True); s.overlay.show_axis_y = G.orig.get('ay',True); s.overlay.show_axis_z = G.orig.get('az',True); s.overlay.show_ortho_grid = G.orig.get('grid',True)
        s.show_gizmo = G.orig.get('gz',True)
    except: pass

def _food_pos():
    for _ in range(200):
        x = random.randint(-GRID_SIZE+1, GRID_SIZE-1); y = random.randint(-GRID_SIZE+1, GRID_SIZE-1)
        if (x,y) not in G.positions and (x,y) not in G.obstacles: break
    if G.food and G.food in bpy.data.objects: bpy.data.objects[G.food].location = (x, y, G.game_z)

def _die():
    G.over = True
    _mat("WallRed", (1.0, 0.1, 0.1), 2.0)
    for nm in G.walls:
        if nm in bpy.data.objects and "WallRed" in bpy.data.materials: bpy.data.objects[nm].data.materials[0] = bpy.data.materials["WallRed"]
    if _prefs().sound_enabled: sfx_die()

def _move():
    if G.paused or G.over or not G.started: return
    G.direction = G.next_dir; dx, dy = G.direction; hx, hy = G.positions[0]; nx, ny = hx+dx, hy+dy
    if abs(nx) >= GRID_SIZE or abs(ny) >= GRID_SIZE: return _die()
    if (nx,ny) in G.positions[:-1] or (nx,ny) in G.obstacles: return _die()
    if G.parts and G.parts[0] in bpy.data.objects: bpy.data.objects[G.parts[0]].location = (nx, ny, G.game_z)
    G.positions.insert(0, (nx,ny))
    ate = False
    if G.food and G.food in bpy.data.objects:
        f = bpy.data.objects[G.food]
        if (nx,ny) == (round(f.location.x), round(f.location.y)):
            G.score += 10; _food_pos(); ate = True
            if _prefs().sound_enabled: sfx_eat()
    if not ate:
        if len(G.positions) > len(G.parts): G.positions.pop()
        for i in range(1, len(G.parts)):
            if i < len(G.positions) and G.parts[i] in bpy.data.objects: bpy.data.objects[G.parts[i]].location = G.positions[i] + (G.game_z,)
    if len(G.positions) > len(G.parts):
        lp = G.positions[-1]; _mat("TailMat", (0.05, 0.6, 0.15), 0.25)
        nm = _obj('C', f"T{len(G.parts)}", lp+(G.game_z,), "TailMat", s=TAIL_SPACING); _link(nm); G.parts.append(nm)

def _anim_food():
    if G.over or not G.food or G.food not in bpy.data.objects: return
    f = bpy.data.objects[G.food]; f.rotation_euler.x += 0.08; f.rotation_euler.z += 0.05; f.location.z = G.game_z + math.sin(time.time()*3.0)*0.2

def _clean(ctx):
    if G.handle:
        try: bpy.types.SpaceView3D.draw_handler_remove(G.handle, 'WINDOW')
        except: pass
        G.handle = None
    if "SnakeCol" in bpy.data.collections:
        for o in list(bpy.data.collections["SnakeCol"].objects): bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.collections.remove(bpy.data.collections["SnakeCol"])
    for pfx in ["Head", "T", "Wall_", "WN", "WS", "WE", "WW", "Food", "Floor", "Obs_"]:
        for obj in list(bpy.data.objects):
            if obj.name.startswith(pfx):
                try: bpy.data.objects.remove(obj, do_unlink=True)
                except: pass
    for mn in ["HeadMat", "TailMat", "FoodMat", "WallMat", "WallRed", "FloorMat"]:
        if mn in bpy.data.materials: bpy.data.materials.remove(bpy.data.materials[mn])
    _unlock(ctx)
    G.active = G.over = G.paused = G.started = False; G.score = 0; G.direction = G.next_dir = (1,0); G.parts = []; G.positions = []; G.obstacles = []; G.food = ""; G.walls = []
    for a in ctx.screen.areas:
        if a.type == 'VIEW_3D': a.tag_redraw()

def _hud(ctx):
    if not G.active: return
    r = ctx.region
    if not r: return
    cx, cy = r.width//2, r.height-50
    blf.size(0, 32); blf.color(0, 0.3,1.0,0.3,1.0); t = f"SCORE: {G.score}"; blf.position(0, cx-len(t)*8, cy, 0); blf.draw(0, t)
    blf.size(0, 18)
    if not G.started: blf.color(0, 1.0,0.8,0.0,1.0); s = "Press any Arrow key to START!"; blf.position(0, cx-len(s)*5, cy-35, 0); blf.draw(0, s)
    elif G.over: blf.color(0, 1.0,0.2,0.2,1.0); s = "GAME OVER - ENTER to restart"; blf.position(0, cx-len(s)*5, cy-35, 0); blf.draw(0, s)
    elif G.paused: blf.color(0, 1.0,0.8,0.0,1.0); s = "PAUSED - P to resume"; blf.position(0, cx-len(s)*5, cy-35, 0); blf.draw(0, s)
    else: blf.color(0, 0.6,0.6,0.6,1.0); s = "Arrows: Move | P: Pause | ESC: Quit"; blf.position(0, cx-len(s)*5, cy-35, 0); blf.draw(0, s)

# ============================================================
# OPERATORS
# ============================================================

class SNAKE_OT_Play(bpy.types.Operator):
    bl_idname = "view3d.snake_play"; bl_label = "▶ PLAY SNAKE!"
    
    def invoke(self, ctx, event):
        if ctx.area.type != 'VIEW_3D' or G.active: return {'CANCELLED'}
        _clean(ctx)
        prefs = _prefs(); G.game_z = 0.0 if prefs.scene_obstacles else 10.0
        G.active = True; G.started = False; G.last_update = time.time()
        _lock(ctx); _col(); _create_materials()
        sp = (0, -GRID_SIZE//2+2)
        G.obstacles = []
        if prefs.scene_obstacles:
            sc = bpy.data.collections.get("SnakeCol")
            for obj in bpy.data.objects:
                if obj.type == 'MESH' and not (sc and obj.name in [o.name for o in sc.objects]):
                    p = (round(obj.location.x), round(obj.location.y))
                    if p not in G.obstacles and p not in G.positions: G.obstacles.append(p)
        _link(_obj('P', "Floor", (0,0,G.game_z-2.5), "FloorMat", s=GRID_SIZE*5.0))
        head_name = _obj('M', "Head", sp+(G.game_z,), "HeadMat", s=0.5); _link(head_name)
        G.parts = [head_name]; G.positions = [sp]; G.direction = G.next_dir = (0,1)
        for i in range(TAIL_START):
            p = (sp[0], sp[1]-(i+1)); G.positions.append(p)
            _link(_obj('C', f"T{i+1}", p+(G.game_z,), "TailMat", s=TAIL_SPACING)); G.parts.append(f"T{i+1}")
        G.food = _obj('Y', "Food", (5,0,G.game_z), "FoodMat", r=0.3, d=0.08); _link(G.food); _food_pos()
        h = GRID_SIZE; L = h*2+0.3; G.walls = []
        for nm, p, sc, rt in [("WN",(0,h,G.game_z),(L,0.3,0.8),(0,0,0)),("WS",(0,-h,G.game_z),(L,0.3,0.8),(0,0,0)),("WE",(h,0,G.game_z),(L,0.3,0.8),(0,0,math.pi/2)),("WW",(-h,0,G.game_z),(L,0.3,0.8),(0,0,math.pi/2))]:
            _link(_obj('C', nm, p, "WallMat", s=1, sc=sc, rt=rt)); G.walls.append(nm)
        G.handle = bpy.types.SpaceView3D.draw_handler_add(_hud, (ctx,), 'WINDOW', 'POST_PIXEL')
        ctx.window_manager.modal_handler_add(self); ctx.area.tag_redraw()
        return {'RUNNING_MODAL'}
    
    def modal(self, ctx, event):
        if not G.active: return {'CANCELLED'}
        if event.type == 'ESC' and event.value == 'PRESS': _clean(ctx); return {'CANCELLED'}
        if event.type in ('LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE') and event.value == 'PRESS': _clean(ctx); return {'CANCELLED'}
        if event.type == 'RET' and event.value == 'PRESS' and G.over: _clean(ctx); bpy.ops.view3d.snake_play('INVOKE_DEFAULT'); return {'CANCELLED'}
        if event.type == 'P' and event.value == 'PRESS' and G.started and not G.over:
            G.paused = not G.paused
            if not G.paused: G.last_update = time.time()
        if not G.paused and not G.over and event.value == 'PRESS':
            d = G.direction
            if event.type == 'UP_ARROW' and d != (0,-1): G.next_dir = (0,1)
            elif event.type == 'DOWN_ARROW' and d != (0,1): G.next_dir = (0,-1)
            elif event.type == 'LEFT_ARROW' and d != (1,0): G.next_dir = (-1,0)
            elif event.type == 'RIGHT_ARROW' and d != (-1,0): G.next_dir = (1,0)
            if not G.started and event.type in ('UP_ARROW','DOWN_ARROW','LEFT_ARROW','RIGHT_ARROW'): G.started = True; G.last_update = time.time()
        if G.started and not G.paused and not G.over:
            if time.time() - G.last_update >= GAME_SPEED: _move(); G.last_update = time.time()
            _anim_food()
        if ctx.area: ctx.area.tag_redraw()
        return {'PASS_THROUGH'}

class SNAKE_PT_Panel(bpy.types.Panel):
    bl_label = "Snake Game"; bl_idname = "SNAKE_PT_panel"; bl_space_type = 'VIEW_3D'; bl_region_type = 'UI'; bl_category = "Snake"
    
    def draw(self, ctx):
        l = self.layout; p = _prefs()
        l.box().label(text="🐍 Snake Game v15.2", icon='PLAY')
        l.separator()
        r = l.row(); r.scale_y = 3.0; r.operator("view3d.snake_play", text="▶ PLAY SNAKE!", icon='PLAY')
        l.separator(); l.box().label(text="💡 Press Arrow key to START", icon='INFO')
        l.separator()
        b = l.box(); b.label(text="👤 Player", icon='USER'); b.prop(p, "player_name", text="Name")
        l.separator()
        b = l.box(); b.label(text="🎮 Options", icon='SETTINGS'); b.prop(p, "view_angle", slider=True); b.prop(p, "graphics_quality"); b.prop(p, "sound_enabled"); b.prop(p, "scene_obstacles")
        l.separator()
        b = l.box(); b.label(text="🎯 Controls:", icon='HAND')
        for t in ["↑↓←→ Arrows: Move & START", "P: Pause", "ESC: Quit", "Enter: Restart", "Click: Quit"]: b.label(text=t)

# ============================================================
# REGISTER
# ============================================================

classes = [SnakePreferences, SNAKE_OT_Play, SNAKE_PT_Panel]

def register():
    for c in classes: bpy.utils.register_class(c)

def unregister():
    for c in reversed(classes): bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()