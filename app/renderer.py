#!/usr/bin/env python3
"""
Grimoria GL-renderer — moderne animatietechniek voor de Pi 5.
Hele scene (nevel, sluier, ogen, orb, sterren, korrel) als fragment-shader
op de V3D-GPU. CPU doet alleen state-machine + tekst-upload bij wijziging.

Render-pipeline per frame:
  1. fragment-shader -> half-res FBO (1920x540)   [GPU]
  2. FBO -> scherm, lineair geupscaled            [GPU]
  3. tekst-quads (titel/caption) op volledige res [GPU]
"""
import glob
import io
import os
import random
import struct
import time

import pygame

try:
    import moderngl
except ImportError as e:
    raise ImportError(f"moderngl ontbreekt: {e}") from None

# ---------------------------------------------------------------- shaders
VS_TRI = """
#version 140
in vec2 in_pos;
out vec2 v_uv;
void main(){
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

FS_SCENE = """
#version 140
in vec2 v_uv;
out vec4 o;

uniform float u_time;
uniform int   u_state;   // 0 SLEEP 1 RETURN 2 WAKING 3 AWAKE 4 TALKING 5 SCARE 6 LAUGH
uniform float u_mouth;   // 0..1 mondopening
uniform vec2  u_eye;     // blik -1..1
uniform float u_lid;     // 0=dicht 1=open
uniform float u_flash;   // bliksemscherm
uniform float u_talk;    // 1 tijdens spreken (orb-puls)
uniform float u_scale;   // zoom bij jumpscare

float hash21(vec2 p){
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float vnoise(vec2 p){
    vec2 i = floor(p); vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}
float fbm(vec2 p){
    float v = 0.0; float a = 0.5;
    for (int i = 0; i < 4; i++){
        v += a * vnoise(p);
        p = p * 2.03 + vec2(11.3, 7.7);
        a *= 0.5;
    }
    return v;
}

void main(){
    float t = u_time;
    vec2 suv = vec2(fract(v_uv.x * 2.0), v_uv.y);   // uv binnen dit scherm
    vec2 sq  = suv * 2.0 - 1.0;
    vec2 q   = vec2(sq.x * 1.7778, sq.y);           // aspect-gecorrigeerd, y omhoog
    float side = step(v_uv.x, 0.5);                 // 1 = links (geest)
    vec3 col;

    if (side > 0.5){
        // ================= scherm 1: Madame Grimoria =================
        float sleeping = (u_state == 0 || u_state == 1) ? 1.0 : 0.0;
        float waking   = (u_state == 2) ? 1.0 : 0.0;
        float scare    = (u_state == 5) ? 1.0 : 0.0;
        float fl = 0.82 + 0.18 * vnoise(vec2(t * 1.6, 3.7));   // kaarslicht-flikker

        // achtergrond-nevel
        float mist = fbm(vec2(q.x * 1.3 + t * 0.05, q.y * 1.3 - t * 0.03));
        col = vec3(0.010, 0.020, 0.014) + vec3(0.015, 0.035, 0.022) * mist * fl;

        // rondwarrelende sintels
        for (int i = 0; i < 22; i++){
            float fi = float(i);
            float sp = 0.014 + 0.020 * hash21(vec2(fi, 1.0));
            float yy = fract(hash21(vec2(fi, 3.0)) + t * sp) * 2.3 - 1.15;
            float xx = (hash21(vec2(fi, 2.0)) * 2.0 - 1.0) * 1.75
                       + sin(t * 0.4 + fi * 1.7) * 0.06;
            vec2 d = q - vec2(xx, yy);
            d.y *= 1.6;
            float dd = dot(d, d);
            float tw = 0.4 + 0.6 * sin(t * 2.6 + fi * 9.1);
            col += vec3(0.45, 0.85, 0.55) * exp(-dd * 2600.0) * 0.30 * tw;
        }

        // ---- de geest ----
        vec2 g = q / u_scale;
        g.x -= sin(t * 0.23) * 0.02;                       // trage drift
        g.y -= sin(t * mix(1.6, 0.7, sleeping)) * (0.012 + 0.010 * sleeping); // adem

        vec2 hc = vec2(0.0, 0.12);                          // hoofd-centrum
        vec2 hr = vec2(0.50, 0.66);
        float e = length((g - hc) / hr);
        float body = 1.0 - smoothstep(0.72, 1.0, e);

        // sluier met noise-vervlochten strengen
        float wenv = 1.0 - smoothstep(0.28, 0.62, abs(g.x));
        float venv = smoothstep(-1.08, -0.30, g.y) * (1.0 - smoothstep(-0.10, 0.05, g.y));
        float warp = fbm(vec2(g.x * 3.5, g.y * 2.0 - t * 0.45));
        float strands = 0.55 + 0.45 * sin(g.x * 17.0 + warp * 5.0 + t * 0.5);
        float veil = wenv * venv * strands;

        float alpha = max(body, veil * 0.85);
        float inner = fbm(g * 2.6 + vec2(t * 0.12, -t * 0.07));
        alpha *= 0.72 + 0.28 * inner;                       // inwendige nevel

        vec3 pale = mix(vec3(0.62, 0.72, 0.62), vec3(0.40, 0.48, 0.42), sleeping);
        pale = mix(pale, vec3(0.62, 0.22, 0.20), max(waking, scare));
        vec3 ghostcol = pale * (0.55 + 0.45 * fl);

        // aurora-gloed achter de geest
        vec2 gv = g - hc;
        float gd = dot(gv, gv);
        vec3 glowc = mix(vec3(0.10, 0.30, 0.18), vec3(0.55, 0.10, 0.08), max(waking, scare));
        col += glowc * exp(-gd * 2.6) * (0.45 + 0.25 * sin(t * 1.1)) * fl;

        col = mix(col, ghostcol, clamp(alpha, 0.0, 1.0) * 0.92);

        // ---- gezicht ----
        vec2 eyeoff = u_eye * vec2(0.075, 0.045);
        for (int s = 0; s < 2; s++){
            float sx = (s == 0) ? -1.0 : 1.0;
            vec2 ep = hc + vec2(sx * 0.27, 0.20);
            vec2 er = vec2(0.155, 0.115);
            vec2 d2 = (g - ep) / er;
            float ee = dot(d2, d2);
            // donkere oogkas
            col = mix(col, vec3(0.02, 0.03, 0.02),
                      (1.0 - smoothstep(0.55, 1.15, ee)) * 0.85);
            // gloeiende pupil (volgt blik)
            float openv = u_lid;
            vec2 pp = g - (ep + eyeoff);
            pp.y /= max(openv, 0.12);
            float pd = dot(pp / 0.075, pp / 0.075);
            vec3 eyecol = mix(vec3(0.75, 1.0, 0.75), vec3(1.0, 0.25, 0.2), scare);
            col += eyecol * exp(-pd * 3.0) * openv * 0.85;
            col += eyecol * exp(-pd * 22.0) * openv * 0.9;
            // slapend: dunne booglijn als ooglid
            float al = (g.y - (ep.y - 0.02)) / 0.012;
            float arc = exp(-al * al)
                        * (1.0 - smoothstep(0.0, 0.16, abs(abs(g.x - ep.x) - 0.09)));
            col = mix(col, vec3(0.05, 0.07, 0.05), arc * sleeping * 0.9);
        }

        // mond
        vec2 mp = vec2(0.0, hc.y - 0.34);
        float mh = mix(0.035, 0.16, u_mouth);
        if (scare > 0.5)  mh = 0.34;
        if (sleeping > 0.5) mh = 0.015;
        vec2 md = (g - mp) / vec2(0.16, mh + 0.02);
        float me = dot(md, md);
        col = mix(col, vec3(0.015, 0.015, 0.015), 1.0 - smoothstep(0.8, 1.15, me));

        // vignette + filmkorrel (harder bij jumpscare)
        float vig = smoothstep(1.65, 0.5, length(sq));
        col *= mix(0.5, 1.0, vig);
        float grain = hash21(v_uv * vec2(3840.0, 1080.0) + fract(t) * 13.7) * 2.0 - 1.0;
        col += grain * (0.018 + 0.14 * scare);
    } else {
        // ================= scherm 2: de orb / spiegel =================
        col = vec3(0.006, 0.012, 0.008);
        float neb = fbm(vec2(q.x * 1.6 - t * 0.015, q.y * 1.6 + t * 0.01));
        col += vec3(0.012, 0.030, 0.018) * neb;

        // twinklende sterren via cel-grid
        vec2 gdim = vec2(46.0, 26.0);
        vec2 gp = (sq * 0.5 + 0.5) * gdim;
        vec2 gid = floor(gp);
        vec2 gf = fract(gp) - 0.5;
        float h = hash21(gid);
        if (h > 0.80){
            vec2 off = vec2(hash21(gid + 7.1), hash21(gid + 3.7)) - 0.5;
            float dd = length(gf - off * 0.6);
            float tw = 0.35 + 0.65 * sin(t * (1.0 + 3.0 * hash21(gid + 9.9)) + h * 40.0);
            col += vec3(0.55, 0.85, 0.65) * exp(-dd * dd * 90.0) * 0.8 * tw
                   * (0.3 + 0.7 * smoothstep(0.9, -0.2, sq.y));
        }

        // pulserende orb (sterker tijdens spreken)
        float pul = 1.0 + (0.10 * u_talk + 0.03) * sin(t * 6.0);
        float r = length((q - vec2(0.0, -0.18)) / pul);
        vec3 oc3 = mix(vec3(0.10, 0.35, 0.16), vec3(0.24, 0.65, 0.28), u_talk);
        for (int i = 1; i <= 6; i++){
            col += oc3 * exp(-r * r * float(i) * 1.35) * 0.10;
        }
        col += vec3(0.80, 0.95, 0.85) * exp(-r * r * 45.0);

        float vig = smoothstep(1.65, 0.5, length(sq));
        col *= mix(0.55, 1.0, vig);
        col += (hash21(v_uv * vec2(3840.0, 1080.0) + fract(t) * 13.7) * 2.0 - 1.0) * 0.015;
    }

    // bliksem-flits en rode waking-puls over beide schermen
    col = mix(col, vec3(1.0), clamp(u_flash, 0.0, 1.0) * 0.75);
    if (u_state == 2){
        col = mix(col, vec3(0.45, 0.05, 0.05), 0.25 + 0.15 * sin(t * 8.0));
    }
    o = vec4(col, 1.0);
}
"""

VS_QUAD = """
#version 140
in vec2 in_pos;
in vec2 in_uv;
out vec2 v_uv;
void main(){
    v_uv = in_uv;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

FS_QUAD = """
#version 140
in vec2 v_uv;
out vec4 o;
uniform sampler2D u_tex;
void main(){
    o = texture(u_tex, v_uv);
}
"""

# ---------------------------------------------------------------- Wan-videolus-modus
# FLF2V-frames (Wan 2.1) als doorlopende tekstuur-animatie: rust-lus (adem,
# naadloos) + jumpscare-lus (palindroom stare->scream->stare). Frame-crossfade
# maakt 16 fps videomateriaal boterzacht op 30 fps canvas.
FS_VIDEO = """
#version 140
in vec2 v_uv;
out vec4 o;

uniform float u_time;
uniform int   u_state;   // 0 SLEEP 1 RETURN 2 WAKING 3 AWAKE 4 TALKING 5 SCARE 6 LAUGH
uniform vec2  u_eye;     // blik -1..1 (micro-parallax)
uniform float u_flash;   // bliksemscherm
uniform float u_scale;   // zoom-punch bij jumpscare
uniform float u_fmix;    // crossfade frame i -> i+1
uniform sampler2D u_texA;
uniform sampler2D u_texB;

float hash21(vec2 p){
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float vnoise(vec2 p){
    vec2 i = floor(p); vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}
float fbm(vec2 p){
    float v = 0.0; float a = 0.5;
    for (int i = 0; i < 4; i++){
        v += a * vnoise(p);
        p = p * 2.03 + vec2(11.3, 7.7);
        a *= 0.5;
    }
    return v;
}

void main(){
    float t = u_time;
    float side = step(v_uv.x, 0.5);                 // 1 = links (Grimoria)
    vec2 suv = vec2(fract(v_uv.x * 2.0), v_uv.y);   // uv binnen dit scherm
    vec2 sq  = suv * 2.0 - 1.0;

    // videoframe: linkerhelft (gezicht) -> scherm 1, rechterhelft (orb) -> scherm 2
    // (geen Y-flip: tostring-upload is al verticaal geflipt, v=suv.y staat recht)
    vec2 vuv = vec2(mix(0.5, 0.0, side) + suv.x * 0.5, suv.y);

    float scare    = (u_state == 5) ? 1.0 : 0.0;
    float sleeping = (u_state <= 1) ? 1.0 : 0.0;

    // video beweegt zelf -> heel subtiel leven eromheen
    float zoom = (1.0 + 0.0035 * sin(t * 0.8)) * u_scale;
    vec2 cuv = (vuv - 0.5) / zoom + 0.5;
    cuv += u_eye * vec2(0.004, -0.003);
    if (scare > 0.5){
        cuv += vec2(vnoise(vec2(t * 30.0 + side * 5.0, 1.0)) - 0.5,
                    vnoise(vec2(7.0, t * 30.0 + side * 5.0)) - 0.5) * 0.011;
    }

    vec3 col = mix(texture(u_texA, cuv).rgb, texture(u_texB, cuv).rgb,
                   smoothstep(0.0, 1.0, u_fmix));

    // kaarslicht-flikker + nevelslierten over het glas
    float fl = 0.90 + 0.10 * vnoise(vec2(t * 2.1 + side * 13.0, 3.7));
    col *= fl;
    float wisp = fbm(vec2(suv.x * 3.0 + t * 0.06 + side * 4.0, suv.y * 2.0 - t * 0.035));
    col += vec3(0.018, 0.036, 0.024) * wisp * 0.5;

    // slapend: gedimd
    col *= mix(1.0, 0.55, sleeping);

    // jumpscare: rode puls
    if (scare > 0.5){
        col = mix(col, col * vec3(1.5, 0.55, 0.45), 0.5);
    }

    float vig = smoothstep(1.62, 0.45, length(sq));
    col *= mix(0.42, 1.0, vig);
    float grain = hash21(v_uv * vec2(3840.0, 1080.0) + fract(t) * 13.7) * 2.0 - 1.0;
    col += grain * (0.012 + 0.10 * scare);

    // bliksem-flits en rode waking-puls
    col = mix(col, vec3(1.0), clamp(u_flash, 0.0, 1.0) * 0.75);
    if (u_state == 2){
        col = mix(col, vec3(0.45, 0.05, 0.05), 0.18 + 0.10 * sin(t * 8.0));
    }
    o = vec4(col, 1.0);
}
"""

# ---------------------------------------------------------------- v2: fase-machine-modus
# Per-state media uit de bibliotheek, rotatie per scherm, wide-canvas met
# bezel-compensatie, overlay-quads. Actief zodra Renderer met v2=... wordt
# gemaakt; zonder die parameter gedraagt hij zich exact als voorheen.
FS_V2 = """
#version 140
in vec2 v_uv;
out vec4 o;

uniform float u_time;
uniform float u_flash;    // bliksemscherm
uniform float u_dim;      // 0..1 dimmen (rust-effect)
uniform float u_scare;    // 0..1 rode puls
uniform float u_flick;    // 0..1 kaarslicht-flikker
uniform float u_wide;     // 0 = split, 1 = doorlopend canvas
uniform vec2  u_bezel;    // wide: overlap-x (fractie), offset-y
uniform vec2  u_rot01;    // rotatie scherm0/scherm1 (radialen)
uniform float u_fmix0;    // crossfade scherm0
uniform float u_fmix1;    // crossfade scherm1
uniform float u_fmixW;    // crossfade wide
uniform float u_vert;     // 0 = schermen naast elkaar, 1 = boven elkaar
uniform float u_swap;     // 1 = volgorde van de schermen gespiegeld
uniform sampler2D u_texA0;
uniform sampler2D u_texB0;
uniform sampler2D u_texA1;
uniform sampler2D u_texB1;
uniform sampler2D u_texAW;
uniform sampler2D u_texBW;

float hash21(vec2 p){
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float vnoise(vec2 p){
    vec2 i = floor(p); vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

// lokale uv 0..1 (y omlaag) roteren rond het midden van het scherm
vec2 rot_uv(vec2 l, float rot){
    vec2 p = l - 0.5;
    float c = cos(rot), s = sin(rot);
    p = mat2(c, -s, s, c) * p;
    return p + 0.5;
}

void main(){
    float t = u_time;
    // scherm-index en within-screen uv, layout-bewust:
    // horizontal: veld gesplitst op x (naast elkaar), vertical: op y (gestapeld)
    float sel, su, sv;
    if (u_vert > 0.5){
        sel = step(0.5, v_uv.y);          // 0 = boven, 1 = onder
        su = v_uv.x;
        sv = fract(v_uv.y * 2.0);
    } else {
        sel = step(0.5, v_uv.x);          // 0 = links, 1 = rechts
        su = fract(v_uv.x * 2.0);
        sv = v_uv.y;
    }
    float side = mix(sel, 1.0 - sel, u_swap);   // logische scherm-index
    float rot = mix(u_rot01.x, u_rot01.y, side);
    float l_x = su;
    float l_y = sv;
    vec2 l = rot_uv(vec2(l_x, l_y), rot);
    float inframe = step(abs(l.x - 0.5), 0.5) * step(abs(l.y - 0.5), 0.5);

    vec3 col;
    if (u_wide > 0.5){
        // doorlopend canvas: u loopt over beide schermen, richting volgt layout
        float ov = u_bezel.x;
        float tu, tv;
        if (u_vert > 0.5){
            tu = clamp(l.x + u_bezel.y, 0.0, 1.0);
            tv = mix(l.y * (0.5 + ov), (0.5 - ov) + l.y * (0.5 + ov), side);
        } else {
            tu = mix(l.x * (0.5 + ov), (0.5 - ov) + l.x * (0.5 + ov), side);
            tv = clamp(l.y + u_bezel.y, 0.0, 1.0);
        }
        vec2 cuv = vec2(mix(tu, 1.0 - tu, u_swap), tv);
        vec3 a = texture(u_texAW, cuv).rgb;
        vec3 b = texture(u_texBW, cuv).rgb;
        col = mix(a, b, smoothstep(0.0, 1.0, u_fmixW)) * inframe;
    } else {
        // split: elk scherm zijn eigen frame-paar (volledige texture)
        vec2 cuv = l;
        vec3 a = mix(texture(u_texA0, cuv).rgb, texture(u_texB0, cuv).rgb,
                     smoothstep(0.0, 1.0, u_fmix0));
        vec3 b = mix(texture(u_texA1, cuv).rgb, texture(u_texB1, cuv).rgb,
                     smoothstep(0.0, 1.0, u_fmix1));
        col = mix(a, b, side) * inframe;
    }

    // leven: flikker + nevelslierten + vignette + korrel
    float fl = mix(1.0, 0.90 + 0.10 * vnoise(vec2(t * 2.1 + side * 13.0, 3.7)), u_flick);
    col *= fl;
    float wisp = vnoise(vec2(su * 3.0 + t * 0.06 + side * 4.0, sv * 2.0 - t * 0.035));
    col += vec3(0.018, 0.036, 0.024) * wisp * 0.5 * u_flick;
    vec2 sq = vec2(su * 2.0 - 1.0, sv * 2.0 - 1.0);
    float vig = smoothstep(1.62, 0.45, length(sq));
    col *= mix(0.42, 1.0, vig);
    if (u_scare > 0.5) col = mix(col, col * vec3(1.5, 0.55, 0.45), 0.5);
    col *= mix(1.0, 0.55, u_dim);
    col += (hash21(v_uv * vec2(3840.0, 1080.0) + fract(t) * 13.7) * 2.0 - 1.0)
           * (0.012 + 0.10 * u_scare);
    col = mix(col, vec3(1.0), clamp(u_flash, 0.0, 1.0) * 0.75);
    o = vec4(col, 1.0);
}
"""

STATE_IDX = {"SLEEP": 0, "RETURN": 1, "WAKING": 2, "AWAKE": 3,
             "TALKING": 4, "SCARE": 5, "LAUGH": 6}

FBO_SCALE = 0.5      # scene rendert op halve res, GPU schaalt op (zacht, mistig)

# ---------------------------------------------------------------- fotorealistische portret-modus
# Twee staande portretten (1080x1920): links Madame Grimoria, rechts De Graaf.
# Zelfde FLUX-vrouw/-man in 4 states (sleep/stare/scream/demon), gouden lijst
# in beeld. Alive-gevoel: adem-zoom, kaarslicht-flikker, nevel, korrel, crossfade.
FS_PORTRAIT = """
#version 140
in vec2 v_uv;
out vec4 o;

uniform float u_time;
uniform int   u_state;   // 0 SLEEP 1 RETURN 2 WAKING 3 AWAKE 4 TALKING 5 SCARE 6 LAUGH
uniform vec2  u_eye;     // blik -1..1 (micro-parallax)
uniform float u_flash;   // bliksemscherm
uniform float u_talk;    // 1 tijdens spreken
uniform float u_scale;   // zoom bij jumpscare
uniform float u_mix;     // crossfade oude->nieuwe portret-texture
uniform float u_omix;    // crossfade orb-textures (onafhankelijk)
uniform sampler2D u_ltexA;
uniform sampler2D u_ltexB;
uniform sampler2D u_rtexA;
uniform sampler2D u_rtexB;

float hash21(vec2 p){
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float vnoise(vec2 p){
    vec2 i = floor(p); vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}
float fbm(vec2 p){
    float v = 0.0; float a = 0.5;
    for (int i = 0; i < 4; i++){
        v += a * vnoise(p);
        p = p * 2.03 + vec2(11.3, 7.7);
        a *= 0.5;
    }
    return v;
}

vec3 sample_tex(sampler2D t, vec2 uv){
    return texture(t, vec2(uv.x, 1.0 - uv.y)).rgb;
}

void main(){
    float t = u_time;
    vec2 suv = vec2(fract(v_uv.x * 2.0), v_uv.y);   // uv binnen dit staande scherm
    vec2 sq  = suv * 2.0 - 1.0;
    float side = step(v_uv.x, 0.5);                 // 1 = links (Grimoria)
    vec3 col;

    // kaarslicht-flikker (gedeeld, per scherm iets anders gefaseerd)
    float fl = 0.88 + 0.12 * vnoise(vec2(t * 2.1 + side * 13.0, 3.7));

    float scare = (u_state == 5) ? 1.0 : 0.0;
    float awake = (u_state >= 3) ? 1.0 : 0.0;

    // adem-zoom + lichte lean-forward als ze wakker zijn
    float breathe = 1.0 + (0.006 + 0.002 * side) * sin(t * (0.85 + 0.13 * side));
    float zoom = breathe * mix(1.0, 1.030, awake) * u_scale;
    vec2 cuv = (suv - 0.5) / zoom + 0.5;
    // micro-parallax: de figuren "leven" mee met de blik
    cuv += u_eye * vec2(0.005, -0.004);
    // shake bij jumpscare (beide schermen, anders gefaseerd)
    if (scare > 0.5){
        cuv += vec2(vnoise(vec2(t * 34.0 + side * 5.0, 1.0)) - 0.5,
                    vnoise(vec2(7.0, t * 34.0 + side * 5.0)) - 0.5) * 0.013;
    }

    vec3 pA = sample_tex(u_ltexA, cuv);
    vec3 pB = sample_tex(u_ltexB, cuv);
    vec3 rA = sample_tex(u_rtexA, cuv);
    vec3 rB = sample_tex(u_rtexB, cuv);
    float m  = smoothstep(0.0, 1.0, u_mix);
    float om = smoothstep(0.0, 1.0, u_omix);
    col = mix(mix(pA, pB, m), mix(rA, rB, om), 1.0 - side);

    // levendheid: flikker + dunne nevelslierten over het glas
    col *= fl;
    float wisp = fbm(vec2(suv.x * 3.0 + t * 0.06 + side * 4.0, suv.y * 2.0 - t * 0.035));
    col += vec3(0.020, 0.038, 0.026) * wisp * 0.55;
    // sinistere groen-gloed rond de randen als ze wakker zijn
    float vig_in = smoothstep(0.32, 0.05, length(sq * vec2(0.62, 1.0)));
    col += vec3(0.030, 0.075, 0.045) * vig_in * awake * (0.6 + 0.4 * sin(t * 1.3));

    // jumpscare: rode puls
    if (scare > 0.5){
        col = mix(col, col * vec3(1.5, 0.55, 0.45), 0.55);
    }
    // praten: zachte puls alsof het kaarslicht meebeweegt met de stem
    col *= 1.0 + 0.045 * u_talk * sin(t * 7.0);

    float vig = smoothstep(1.62, 0.45, length(sq));
    col *= mix(0.42, 1.0, vig);
    float grain = hash21(v_uv * vec2(2160.0, 1920.0) + fract(t) * 13.7) * 2.0 - 1.0;
    col += grain * (0.014 + 0.11 * scare);

    // bliksem-flits en rode waking-puls over beide schermen
    col = mix(col, vec3(1.0), clamp(u_flash, 0.0, 1.0) * 0.75);
    if (u_state == 2){
        col = mix(col, vec3(0.45, 0.05, 0.05), 0.22 + 0.13 * sin(t * 8.0));
    }
    o = vec4(col, 1.0);
}
"""

# state -> expressie; ladder met tussenstappen voor zachte overgangen
PORTRAIT_TEX = {"SLEEP": "sleep", "RETURN": "sleep", "WAKING": "drowsy",
                "AWAKE": "stare", "TALKING": "smirk", "SCARE": "demon",
                "LAUGH": "scream"}
# bij elk doel worden tussenstappen doorlopen (fade-keten)
PORTRAIT_CHAIN = {"sleep": ["drowsy", "sleep"], "drowsy": ["drowsy"],
                  "stare": ["stare"], "smirk": ["smirk"], "angry": ["angry"],
                  "scream": ["scream"],
                  "demon": ["angry", "scream", "demon"]}
ORB_DEMON_STATES = ("SCARE", "LAUGH")
# orb morft doorlopend tussen varianten: 3 rustige rook-fases, 2 demonische
ORB_CALM = ("orb1", "orb2", "orb3")
ORB_DEMON = ("orb4", "orb5")


class Renderer:
    def __init__(self, win_size, require=310, v2=False):
        self.win_w, self.win_h = win_size
        if os.environ.get("GRIMORIA_EGL"):
            # headless-testmodus: offscreen EGL i.p.v. window-context
            self.ctx = moderngl.create_standalone_context(backend="egl",
                                                          require=require)
        else:
            self.ctx = moderngl.create_context(require=require)
        self.gl_version = self.ctx.info.get("GL_VERSION", "?") + " / " \
            + self.ctx.info.get("GL_RENDERER", "?")
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA

        # fullscreen driehoek voor de scene
        self.prog = self.ctx.program(vertex_shader=VS_TRI, fragment_shader=FS_SCENE)
        buf = self.ctx.buffer(struct.pack("6f", -1, -1, 3, -1, -1, 3))
        self.vao = self.ctx.vertex_array(self.prog, buf, "in_pos")
        self._tri_buf = buf

        # half-res FBO
        fw, fh = int(win_size[0] * FBO_SCALE), int(win_size[1] * FBO_SCALE)
        self.fbo_tex = self.ctx.texture((fw, fh), 4)
        self.fbo_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.fbo = self.ctx.framebuffer(color_attachments=[self.fbo_tex])

        # upscale-programma
        self.up_prog = self.ctx.program(vertex_shader=VS_TRI.replace(
            "out vec2 v_uv;", "out vec2 v_uv;"), fragment_shader=FS_QUAD)
        self.up_vao = self.ctx.vertex_array(self.up_prog, buf, "in_pos")
        self.up_prog["u_tex"].value = 0

        # tekst-programma
        self.txt_prog = self.ctx.program(vertex_shader=VS_QUAD, fragment_shader=FS_QUAD)
        self.txt_prog["u_tex"].value = 1

        # fonts (zelfde maten als software-versie)
        self.f_big = pygame.font.SysFont("dejavuserifbold", 110)
        self.f_mid = pygame.font.SysFont("dejavuserifbold", 64)
        self.f_cap = pygame.font.SysFont("dejavusans", 44)

        self._title = None          # (texture, w, h)
        self._cap_key = None
        self._cap_vao = None
        self._cap_h = 0
        self.t = 0.0

        # ---- v2: fase-machine-modus (per-state bibliotheek-media) ----
        self.v2 = None
        self._tex_cache = {}        # (mid, idx) -> texture
        self._font_cache = {}       # size -> Font
        self._ovl_cache = {}        # sleutel -> (texture, vao)
        self._black = self.ctx.texture((2, 2), 3, b"\x00\x00\x00" * 4)
        self._black.filter = (moderngl.LINEAR, moderngl.LINEAR)
        if v2:
            try:
                self.prog2 = self.ctx.program(vertex_shader=VS_TRI,
                                              fragment_shader=FS_V2)
                self.vao2 = self.ctx.vertex_array(self.prog2, buf, "in_pos")
                for uni, unit in (("u_texA0", 0), ("u_texB0", 1),
                                  ("u_texA1", 2), ("u_texB1", 3),
                                  ("u_texAW", 4), ("u_texBW", 5)):
                    self.prog2[uni].value = unit
                self.v2 = {"t0": time.time()}
                print("[renderer] v2 fase-machine-modus AAN")
            except Exception as e:
                print(f"[renderer] v2-modus faalt, legacy-modus: {e}")
                self.v2 = None

        # ---- Wan-videolus-modus (FLF2V-frames, sterkste realisme) ----
        # (in v2-modus overbodig: die gebruikt bibliotheek-media; scheelt
        #  ~70 MB GPU-geheugen en laadtijd op de Pi)
        self.video = None
        if not v2 and not os.environ.get("GRIMORIA_NO_VIDEO"):
            try:
                vdir = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "media", "loops", "frames"))

                def laad_frames(sub, minimum):
                    bestanden = sorted(glob.glob(os.path.join(vdir, sub, "f*.png")))
                    if len(bestanden) < minimum:
                        return None
                    texs = []
                    for b in bestanden:
                        surf = pygame.image.load(b).convert(24)
                        tx = self.ctx.texture(
                            surf.get_size(), 3,
                            pygame.image.tostring(surf, "RGB", True))
                        tx.filter = (moderngl.LINEAR, moderngl.LINEAR)
                        texs.append(tx)
                    return texs

                rust = laad_frames("rust", 40)
                jump = laad_frames("jump", 30)
                if rust and jump:
                    self.progv = self.ctx.program(vertex_shader=VS_TRI,
                                                  fragment_shader=FS_VIDEO)
                    self.vaov = self.ctx.vertex_array(self.progv, buf, "in_pos")
                    self.progv["u_texA"].value = 0
                    self.progv["u_texB"].value = 1
                    # rust-lus loopt vrij door; jump start bij SCARE/HAUGH
                    self.video = {"rust": rust, "jump": jump,
                                  "t_rust": time.time(), "t0_jump": None}
                    print(f"[renderer] video-lusmodus AAN "
                          f"({len(rust)} rust + {len(jump)} jump frames)")
                else:
                    print(f"[renderer] video-lusmodus uit: frames incompleet "
                          f"(rust={len(rust) if rust else 0}, "
                          f"jump={len(jump) if jump else 0})")
            except Exception as e:
                self.video = None
                print(f"[renderer] video-lusmodus uit: {e}")

        # ---- fotorealistische portret-modus (FLUX-beelden, fallback) ----
        self.portrait = None
        try:
            if self.video is not None or v2:
                raise RuntimeError("video-lusmodus actief, portret overgeslagen")
            pdir = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "media"))
            # crop's: gezicht vult héét linkerscherm, orb centraal op rechts;
            # alle varianten delen dezelfde layout (inpaint), dus 1 crop
            gezicht_crop = (1060, 180, 1940, 680)
            orb_crop = (234, 120, 1734, 970)

            def laad(pat, crop):
                hits = sorted(glob.glob(os.path.join(pdir, pat)))
                if not hits:
                    return None
                surf = pygame.image.load(hits[-1]).convert(24)
                surf = pygame.transform.smoothscale(surf.crop(crop), (960, 544))
                tx = self.ctx.texture(
                    (960, 544), 3, pygame.image.tostring(surf, "RGB", True))
                tx.filter = (moderngl.LINEAR, moderngl.LINEAR)
                return tx

            tex = {}
            for k in ("sleep", "drowsy", "stare", "smirk",
                      "angry", "scream", "demon"):
                t = laad(f"gp_{k}_*.png", gezicht_crop)
                if t:
                    tex[k] = t
            for k in ("orb1", "orb2", "orb3", "orb4", "orb5"):
                t = laad(f"grimoria_{k}_*.png", orb_crop)
                if t:
                    tex[k] = t
            if len(tex) == 12:
                self.progp = self.ctx.program(vertex_shader=VS_TRI,
                                              fragment_shader=FS_PORTRAIT)
                self.vaop = self.ctx.vertex_array(self.progp, buf, "in_pos")
                self.ptex = tex
                self.progp["u_ltexA"].value = 0
                self.progp["u_ltexB"].value = 1
                self.progp["u_rtexA"].value = 2
                self.progp["u_rtexB"].value = 3
                # crossfade-state: portret-keten + doorlopende orb-morf
                self.portrait = {"cur": "sleep", "prev": "sleep", "f": 1.0,
                                 "doel": "sleep", "keten": [],
                                 "ocur": "orb1", "oprev": "orb1", "of": 1.0,
                                 "demon": False}
                print("[renderer] portret-modus AAN (12 beelden)")
            else:
                print(f"[renderer] portret-modus uit: {len(tex)}/12 beelden")
        except Exception as e:
            self.portrait = None
            print(f"[renderer] portret-modus uit: {e}")

    
    # ------------------------------------------------ v2: media-textures
    def _v2_tex(self, mid, idx):
        """Texture voor (media-id, frame-index), lazy geladen en gecached."""
        key = (mid, idx)
        tx = self._tex_cache.get(key)
        if tx is not None:
            return tx
        paths = self._v2_frames(mid) if mid else None
        path = paths[idx] if paths and 0 <= idx < len(paths) else None
        if not path:
            return self._black
        try:
            surf = pygame.image.load(path).convert(24)
            tx = self.ctx.texture(
                surf.get_size(), 3, pygame.image.tostring(surf, "RGB", True))
            tx.filter = (moderngl.LINEAR, moderngl.LINEAR)
        except Exception as e:
            print(f"[renderer] texture-fout {path}: {e}")
            return self._black
        self._tex_cache[key] = tx
        return tx

    def set_v2_frames_source(self, fn):
        """fn(media_id) -> lijst frame-padbestanden (of None). Met korte
        TTL-cache: listdir/sort per frame-render is zonde van de CPU."""
        self._v2_frames_fn = fn
        self._paths_cache = {}      # mid -> (paden, timestamp)

    def _v2_frames(self, mid):
        fn = getattr(self, "_v2_frames_fn", None)
        if fn is None:
            return None
        hit = self._paths_cache.get(mid)
        now = time.time()
        if hit and now - hit[1] < 1.0:
            return hit[0]
        paths = fn(mid)
        self._paths_cache[mid] = (paths, now)
        return paths

    def _v2_evict(self, huidige_mids):
        """Gooi textures van media die niet meer in de actieve set zitten."""
        for key in [k for k in list(self._tex_cache) if k[0] not in huidige_mids]:
            try:
                self._tex_cache.pop(key).release()
            except Exception:
                self._tex_cache.pop(key, None)

    def _font(self, size):
        f = self._font_cache.get(size)
        if f is None:
            f = pygame.font.SysFont("dejavuserifbold", int(max(8, size)))
            self._font_cache[size] = f
        return f

    def _bake_overlay_item(self, text, x, y, size, color, screen):
        """Eén overlay-item -> (texture, vao). x,y = centrum in fracties van
        het scherm (fysieke orientatie, dus zonder rotatie)."""
        col = pygame.Color(color)
        font = self._font(size)
        surf = font.render(text, True, (col.r, col.g, col.b))
        pad = max(6, int(size) // 5)
        bg = pygame.Surface((surf.get_width() + 2 * pad,
                             surf.get_height() + pad), pygame.SRCALPHA)
        bg.blit(surf, (pad, pad // 2))
        tex = self._surface_to_tex(bg)
        w, h = bg.get_size()
        vert = getattr(self, "_layout_vert", False)
        swap = getattr(self, "_layout_swap", False)
        scr = (1 - screen) if swap else screen
        if vert:
            cx = x * self.win_w
            cy = scr * self.win_h + y * (self.win_h // 2)
        else:
            cx = scr * (self.win_w // 2) + x * (self.win_w // 2)
            cy = y * self.win_h
        vao = self._quad_vao(cx - w // 2, cy - h // 2, w, h)
        return tex, vao

    def render_overlay(self, items):
        """Teken overlay-items op volledige resolutie (na de upscale)."""
        if self.ctx.screen is None:
            return
        for it in items:
            key = (it.get("text", ""), round(it.get("x", .5), 4),
                   round(it.get("y", .5), 4), int(it.get("size", 44)),
                   it.get("color", "#ffffff"), int(it.get("screen", 0)))
            hit = self._ovl_cache.get(key)
            if hit is None:
                if len(self._ovl_cache) > 64:      # cache begrenzen
                    for k in list(self._ovl_cache)[:32]:
                        t, v = self._ovl_cache.pop(k)
                        try:
                            t.release(); v.release()
                        except Exception:
                            pass
                hit = self._bake_overlay_item(
                    key[0], key[1], key[2], key[3], key[4], key[5])
                self._ovl_cache[key] = hit
            tex, vao = hit
            tex.use(1)
            vao.render(moderngl.TRIANGLES)

# ------------------------------------------------------------ text
    def _surface_to_tex(self, surf):
        data = pygame.image.tostring(surf, "RGBA", True)
        tex = self.ctx.texture(surf.get_size(), 4, data)
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        return tex

    def _bake_title(self):
        t1 = self.f_big.render("MADAME GRIMORIA", True, (190, 220, 190))
        t2 = self.f_mid.render("de spiegel zijner zielen", True, (120, 160, 125))
        pad = 24
        bg = pygame.Surface((max(t1.get_width(), t2.get_width()) + 2 * pad,
                             t1.get_height() + t2.get_height() + 3 * pad),
                            pygame.SRCALPHA)
        bg.blit(t1, ((bg.get_width() - t1.get_width()) // 2, pad))
        bg.blit(t2, ((bg.get_width() - t2.get_width()) // 2,
                     t1.get_height() + 2 * pad))
        self._title = (self._surface_to_tex(bg), bg.get_width(), bg.get_height())

    def _bake_caption(self, text):
        """Caption als texture + quads op beide schermen onderaan."""
        words = text.split()
        lines, cur = [], ""
        maxw = self.win_w // 2 - 400
        for w_ in words:
            if self.f_cap.size(cur + " " + w_)[0] > maxw and cur:
                lines.append(cur)
                cur = w_
            else:
                cur = (cur + " " + w_).strip()
        lines.append(cur)
        lines = lines[:2]
        if not lines:
            self._cap_vao = None
            return
        lh = 64
        total_h = len(lines) * lh + 36
        bg = pygame.Surface((self.win_w // 2, total_h), pygame.SRCALPHA)
        y = 18
        for ln in lines:
            rt = self.f_cap.render(ln, True, (225, 235, 220))
            bg.blit(rt, ((bg.get_width() - rt.get_width()) // 2, y))
            y += lh
        tex = self._surface_to_tex(bg)
        w, h = bg.get_size()
        cx = self.win_w // 2
        cy = self.win_h - 140 - h // 2
        verts = b""
        for sx in (0, cx):                      # zelfde beeld op beide schermen
            x0, x1 = sx + (cx - w) // 2, sx + (cx - w) // 2 + w
            y0, y1 = cy - h // 2, cy + h // 2
            nx0, nx1 = x0 / (self.win_w / 2) - 1, x1 / (self.win_w / 2) - 1
            ny0, ny1 = 1 - y0 / (self.win_h / 2), 1 - y1 / (self.win_h / 2)
            quad = [
                (nx0, ny0, 0, 1), (nx1, ny0, 1, 1), (nx1, ny1, 1, 0),
                (nx0, ny0, 0, 1), (nx1, ny1, 1, 0), (nx0, ny1, 0, 0),
            ]
            for v in quad:
                verts += struct.pack("4f", *v)
        vbo = self.ctx.buffer(verts)
        self._cap_vao = self.ctx.vertex_array(
            self.txt_prog, [(vbo, "2f 2f", "in_pos", "in_uv")])
        self._cap_tex = tex
        self._cap_h = h

    def _quad_vao(self, x, y, w, h):
        """VAO voor texture op pixelpositij (top-left) in het hele venster."""
        x0, x1, y0, y1 = x, x + w, y, y + h
        nx0, nx1 = x0 / (self.win_w / 2) - 1, x1 / (self.win_w / 2) - 1
        ny0, ny1 = 1 - y0 / (self.win_h / 2), 1 - y1 / (self.win_h / 2)
        quad = [
            (nx0, ny0, 0, 1), (nx1, ny0, 1, 1), (nx1, ny1, 1, 0),
            (nx0, ny0, 0, 1), (nx1, ny1, 1, 0), (nx0, ny1, 0, 0),
        ]
        vbo = self.ctx.buffer(struct.pack("24f", *[c for v in quad for c in v]))
        return self.ctx.vertex_array(self.txt_prog,
                                     [(vbo, "2f 2f", "in_pos", "in_uv")])

    # ------------------------------------------------------------ frame
    def render(self, grim):
        self.t += 1.0 / 30.0
        if self.v2 is not None and hasattr(grim, "v2_frame_info"):
            self._render_v2(grim)
        else:
            self._render_scene(grim)

    def _render_v2(self, grim):
        """v2-pad: per-state bibliotheek-media, rotatie/wide per scherm."""
        import math as _math
        info = grim.v2_frame_info()          # mode/mids/effects/bezel/rot
        scr = info.get("screens", {})
        rot0 = _math.radians(float(scr.get("rot0", 0.0)))
        rot1 = _math.radians(float(scr.get("rot1", 0.0)))
        wide = 1.0 if info.get("mode") == "wide" else 0.0
        bez = info.get("bezel", (0.0, 0.0))
        fx = info.get("effects", {})
        fps_v = float(info.get("fps", 16.0))

        v = self.v2
        if info.get("reset_t0"):
            v["t0"] = time.time()
        fr = (time.time() - v["t0"]) * fps_v

        up = self.prog2
        up["u_time"].value = self.t
        up["u_flash"].value = float(grim.flash)
        up["u_dim"].value = float(fx.get("dim", 0.0))
        up["u_scare"].value = 1.0 if fx.get("scare") else 0.0
        up["u_flick"].value = float(fx.get("flick", 1.0))
        up["u_wide"].value = wide
        up["u_bezel"].value = (float(bez[0]), float(bez[1]))
        up["u_rot01"].value = (rot0, rot1)
        lay = info.get("layout", {}) or {}
        up["u_vert"].value = 1.0 if lay.get("mode") == "vertical" else 0.0
        up["u_swap"].value = 1.0 if lay.get("swap") else 0.0

        def pair(mid, u_fmix_name, unit_a, unit_b):
            paths = self._v2_frames(mid) if mid else None
            n = len(paths) if paths else 0
            if n == 0:
                self._black.use(unit_a)
                self._black.use(unit_b)
                up[u_fmix_name].value = 0.0
                return set()
            loop = max(1, n - 1)
            fi = int(fr) % loop
            fj = (fi + 1) % loop
            up[u_fmix_name].value = float(fr - int(fr))
            self._v2_tex(mid, fi).use(unit_a)
            self._v2_tex(mid, fj).use(unit_b)
            return {mid}

        nodig = set()
        if wide > 0.5:
            nodig |= pair(info.get("mids", {}).get("all"), "u_fmixW", 4, 5)
            self._black.use(0); self._black.use(1)
            self._black.use(2); self._black.use(3)
            up["u_fmix0"].value = 0.0
            up["u_fmix1"].value = 0.0
        else:
            mids = info.get("mids", {})
            nodig |= pair(mids.get("0"), "u_fmix0", 0, 1)
            nodig |= pair(mids.get("1"), "u_fmix1", 2, 3)
            self._black.use(4); self._black.use(5)
            up["u_fmixW"].value = 0.0
        self._v2_evict(nodig)

        # 1) scene naar half-res FBO
        self.fbo.use()
        self.vao2.render(moderngl.TRIANGLES)
        # 2) upscale naar scherm
        if self.ctx.screen is not None:
            self.ctx.screen.use()
            self.fbo_tex.use(0)
            self.up_vao.render(moderngl.TRIANGLES)
        # 3) overlay-items op volledige resolutie (fysieke orientatie)
        lay2 = info.get("layout", {}) or {}
        self._layout_vert = lay2.get("mode") == "vertical"
        self._layout_swap = bool(lay2.get("swap"))
        self.render_overlay(info.get("overlay", []))

    def _render_scene(self, grim):
        st = STATE_IDX.get(grim.state, 0)
        sleeping = grim.state in ("SLEEP", "RETURN")
        lid = 0.06 if sleeping else (max(0.06, 1.0 - max(grim.blink, 0) / 0.18)
                                     if grim.blink > 0 else 1.0)
        mouth = grim.mouth
        if grim.state == "SCARE":
            mouth = 1.0
        elif grim.state == "LAUGH":
            mouth = 0.55
        elif sleeping:
            mouth = 0.0
        scale = 1.0
        if grim.state == "SCARE":
            scale = 1.0 + min(1.4, (time.time() - grim.t0) * 1.2)

        # ---- Wan-videolus-modus ----
        if self.video is not None:
            v = self.video
            FPS_V = 16.0
            if grim.state in ("SCARE", "LAUGH"):
                # jump-lus start bij binnenkomst SCARE (t0 gezet door state-machine)
                if v["t0_jump"] is None or v["t0_jump"] < grim.t0 - 0.05:
                    v["t0_jump"] = grim.t0
                fr = (time.time() - v["t0_jump"]) * FPS_V
                frames = v["jump"]
            else:
                v["t0_jump"] = None
                fr = (time.time() - v["t_rust"]) * FPS_V
                frames = v["rust"]
            n = len(frames)
            loop = n - 1                    # laatste frame == eerste (naadloos)
            fi = int(fr) % loop
            fj = (fi + 1) % loop
            fmix = fr - int(fr)
            up = self.progv
            up["u_time"].value = self.t
            up["u_state"].value = st
            up["u_eye"].value = tuple(float(x) for x in grim.eye)
            up["u_flash"].value = float(grim.flash)
            up["u_scale"].value = float(scale)
            up["u_fmix"].value = float(fmix)
            frames[fi].use(0)
            frames[fj].use(1)

            self.fbo.use()
            self.vaov.render(moderngl.TRIANGLES)

        # ---- fotorealistische portret-modus ----
        elif self.portrait is not None:
            dt = 1.0 / 30.0
            p = self.portrait
            # expressie-keten: loop tussenstappen door richting het doel
            doel = PORTRAIT_TEX.get(grim.state, "stare")
            if doel != p["doel"]:
                p["doel"] = doel
                p["keten"] = [s for s in PORTRAIT_CHAIN[doel] if s != p["cur"]]
            if p["keten"] and p["f"] >= 1.0:
                p["prev"], p["cur"], p["f"] = p["cur"], p["keten"].pop(0), 0.0
            # traag ontwaken, snel bij schrik
            snelheid = 2.6 if grim.state in ("SCARE", "LAUGH") else 1.1
            p["f"] = min(1.0, p["f"] + dt * snelheid)
            # orb: doorlopende morf tussen varianten binnen de juiste mood;
            # achtergrond blijft identiek (inpaint), dus alleen de rook leeft
            demon = grim.state in ORB_DEMON_STATES
            if demon != p["demon"]:
                p["demon"] = demon
                p["oprev"], p["ocur"], p["of"] = p["ocur"], \
                    random.choice(ORB_DEMON if demon else ORB_CALM), 0.0
            elif p["of"] >= 1.0:
                p["oprev"] = p["ocur"]
                keus = ORB_DEMON if demon else ORB_CALM
                p["ocur"] = random.choice([k for k in keus if k != p["ocur"]])
                p["of"] = 0.0
            p["of"] = min(1.0, p["of"] + dt * (0.9 if demon else 0.28))

            up = self.progp
            up["u_time"].value = self.t
            up["u_state"].value = st
            up["u_eye"].value = tuple(float(v) for v in grim.eye)
            up["u_flash"].value = float(grim.flash)
            up["u_talk"].value = 1.0 if grim.state == "TALKING" else 0.0
            up["u_scale"].value = float(scale)
            up["u_mix"].value = float(p["f"])
            up["u_omix"].value = float(p["of"])
            self.ptex[p["prev"]].use(0)
            self.ptex[p["cur"]].use(1)
            self.ptex[p["oprev"]].use(2)
            self.ptex[p["ocur"]].use(3)

            self.fbo.use()
            self.vaop.render(moderngl.TRIANGLES)
        else:
            u = self.prog
            u["u_time"].value = self.t
            u["u_state"].value = st
            u["u_mouth"].value = float(mouth)
            u["u_eye"].value = tuple(float(v) for v in grim.eye)
            u["u_lid"].value = float(lid)
            u["u_flash"].value = float(grim.flash)
            u["u_talk"].value = 1.0 if grim.state == "TALKING" else 0.0
            u["u_scale"].value = float(scale)

            # 1) scene naar half-res FBO
            self.fbo.use()
            self.vao.render(moderngl.TRIANGLES)

        # 2) upscale naar scherm
        if self.ctx.screen is not None:
            self.ctx.screen.use()
            self.fbo_tex.use(0)
            self.up_vao.render(moderngl.TRIANGLES)

        # 3) tekst op volledige resolutie
        if grim.state != "SCARE":
            if self._title is None:
                self._bake_title()
            tex, tw, th = self._title
            if not hasattr(self, "_title_vao"):
                cx = self.win_w // 2 + self.win_w // 4      # midden rechts scherm
                self._title_vao = self._quad_vao(cx - tw // 2, 240, tw, th)
            tex.use(1)
            self._title_vao.render(moderngl.TRIANGLES)
        cap = grim.caption if grim.state in ("TALKING", "AWAKE", "WAKING") else ""
        if cap != self._cap_key:
            self._cap_key = cap
            self._bake_caption(cap) if cap else setattr(self, "_cap_vao", None)
        if cap and self._cap_vao is not None:
            self._cap_tex.use(1)
            self._cap_vao.render(moderngl.TRIANGLES)

    # ------------------------------------------------------------ debug
    def png_bytes(self):
        """FBO-inhoud als PNG (wat de geest laat zien, zonder tekst)."""
        data = self.fbo.read(components=3, alignment=1)
        mk = getattr(pygame.image, "frombytes", None) or pygame.image.fromstring
        surf = mk(data, self.fbo.size, "RGB")
        surf = pygame.transform.flip(surf, False, True)
        bio = io.BytesIO()
        pygame.image.save(surf, bio)
        return bio.getvalue()

    def release(self):
        try:
            self.fbo.release()
            self.fbo_tex.release()
            self.vao.release()
            self.up_vao.release()
            self.prog.release()
            self.up_prog.release()
            self.txt_prog.release()
            self.ctx.release()
        except Exception:
            pass
