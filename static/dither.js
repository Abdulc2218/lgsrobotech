/* Dithered wave background for LGS WT Sports Fest 2026.
   A vanilla-WebGL2 port of the React Bits <Dither /> shader (wave FBM +
   8x8 Bayer dithering) so it runs on a plain HTML page with no React/Three.
   Colors are tuned to the site's stadium theme. */
(function () {
  var canvas = document.getElementById("dither-bg");
  if (!canvas) return;
  var gl = canvas.getContext("webgl2", { antialias: false, alpha: false });
  if (!gl) return; // no WebGL2 → the navy page background shows instead

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- tuning (theme-matched) -------------------------------------------
  var WAVE_SPEED = 0.035;
  var WAVE_FREQUENCY = 3.0;
  var WAVE_AMPLITUDE = 0.3;
  var WAVE_COLOR = [0.11, 0.45, 0.78];        // sporty electric blue
  var BG_COLOR = [0.035, 0.086, 0.157];       // stadium navy
  var COLOR_NUM = 4.0;
  var PIXEL_SIZE = 2.0;
  var MOUSE_RADIUS = 0.32;

  var vertSrc = "#version 300 es\n" +
    "in vec2 position;\n" +
    "void main(){ gl_Position = vec4(position, 0.0, 1.0); }\n";

  var fragSrc = "#version 300 es\n" +
"precision highp float;\n" +
"out vec4 fragColor;\n" +
"uniform vec2 resolution;\n" +
"uniform float time;\n" +
"uniform float waveSpeed;\n" +
"uniform float waveFrequency;\n" +
"uniform float waveAmplitude;\n" +
"uniform vec3 waveColor;\n" +
"uniform vec3 backgroundColor;\n" +
"uniform vec2 mousePos;\n" +
"uniform int enableMouseInteraction;\n" +
"uniform float mouseRadius;\n" +
"uniform float colorNum;\n" +
"uniform float pixelSize;\n" +
"vec4 mod289(vec4 x){ return x - floor(x * (1.0/289.0)) * 289.0; }\n" +
"vec4 permute(vec4 x){ return mod289(((x*34.0)+1.0)*x); }\n" +
"vec4 taylorInvSqrt(vec4 r){ return 1.79284291400159 - 0.85373472095314 * r; }\n" +
"vec2 fade(vec2 t){ return t*t*t*(t*(t*6.0-15.0)+10.0); }\n" +
"float cnoise(vec2 P){\n" +
"  vec4 Pi = floor(P.xyxy) + vec4(0.0,0.0,1.0,1.0);\n" +
"  vec4 Pf = fract(P.xyxy) - vec4(0.0,0.0,1.0,1.0);\n" +
"  Pi = mod289(Pi);\n" +
"  vec4 ix = Pi.xzxz; vec4 iy = Pi.yyww;\n" +
"  vec4 fx = Pf.xzxz; vec4 fy = Pf.yyww;\n" +
"  vec4 i = permute(permute(ix) + iy);\n" +
"  vec4 gx = fract(i * (1.0/41.0)) * 2.0 - 1.0;\n" +
"  vec4 gy = abs(gx) - 0.5;\n" +
"  vec4 tx = floor(gx + 0.5);\n" +
"  gx = gx - tx;\n" +
"  vec2 g00 = vec2(gx.x, gy.x); vec2 g10 = vec2(gx.y, gy.y);\n" +
"  vec2 g01 = vec2(gx.z, gy.z); vec2 g11 = vec2(gx.w, gy.w);\n" +
"  vec4 norm = taylorInvSqrt(vec4(dot(g00,g00), dot(g01,g01), dot(g10,g10), dot(g11,g11)));\n" +
"  g00 *= norm.x; g01 *= norm.y; g10 *= norm.z; g11 *= norm.w;\n" +
"  float n00 = dot(g00, vec2(fx.x, fy.x));\n" +
"  float n10 = dot(g10, vec2(fx.y, fy.y));\n" +
"  float n01 = dot(g01, vec2(fx.z, fy.z));\n" +
"  float n11 = dot(g11, vec2(fx.w, fy.w));\n" +
"  vec2 fade_xy = fade(Pf.xy);\n" +
"  vec2 n_x = mix(vec2(n00, n01), vec2(n10, n11), fade_xy.x);\n" +
"  return 2.3 * mix(n_x.x, n_x.y, fade_xy.y);\n" +
"}\n" +
"float fbm(vec2 p){\n" +
"  float value = 0.0; float amp = 1.0; float freq = waveFrequency;\n" +
"  for(int i=0;i<4;i++){ value += amp * abs(cnoise(p)); p *= freq; amp *= waveAmplitude; }\n" +
"  return value;\n" +
"}\n" +
"float pattern(vec2 p){ vec2 p2 = p - time * waveSpeed; return fbm(p + fbm(p2)); }\n" +
"const float bayer[64] = float[64](\n" +
" 0.0/64.0,48.0/64.0,12.0/64.0,60.0/64.0, 3.0/64.0,51.0/64.0,15.0/64.0,63.0/64.0,\n" +
"32.0/64.0,16.0/64.0,44.0/64.0,28.0/64.0,35.0/64.0,19.0/64.0,47.0/64.0,31.0/64.0,\n" +
" 8.0/64.0,56.0/64.0, 4.0/64.0,52.0/64.0,11.0/64.0,59.0/64.0, 7.0/64.0,55.0/64.0,\n" +
"40.0/64.0,24.0/64.0,36.0/64.0,20.0/64.0,43.0/64.0,27.0/64.0,39.0/64.0,23.0/64.0,\n" +
" 2.0/64.0,50.0/64.0,14.0/64.0,62.0/64.0, 1.0/64.0,49.0/64.0,13.0/64.0,61.0/64.0,\n" +
"34.0/64.0,18.0/64.0,46.0/64.0,30.0/64.0,33.0/64.0,17.0/64.0,45.0/64.0,29.0/64.0,\n" +
"10.0/64.0,58.0/64.0, 6.0/64.0,54.0/64.0, 9.0/64.0,57.0/64.0, 5.0/64.0,53.0/64.0,\n" +
"42.0/64.0,26.0/64.0,38.0/64.0,22.0/64.0,41.0/64.0,25.0/64.0,37.0/64.0,21.0/64.0);\n" +
"void main(){\n" +
"  vec2 fragCoord = gl_FragCoord.xy;\n" +
"  vec2 pixCoord = floor(fragCoord / pixelSize) * pixelSize + pixelSize*0.5;\n" +
"  vec2 uv = pixCoord / resolution.xy;\n" +
"  uv -= 0.5; uv.x *= resolution.x / resolution.y;\n" +
"  float f = pattern(uv);\n" +
"  if(enableMouseInteraction == 1){\n" +
"    vec2 mouseNDC = (mousePos / resolution - 0.5) * vec2(1.0, -1.0);\n" +
"    mouseNDC.x *= resolution.x / resolution.y;\n" +
"    float dist = length(uv - mouseNDC);\n" +
"    float effect = 1.0 - smoothstep(0.0, mouseRadius, dist);\n" +
"    f -= 0.5 * effect;\n" +
"  }\n" +
"  vec3 col = mix(backgroundColor, waveColor, clamp(f, 0.0, 1.0));\n" +
"  vec2 scaledCoord = floor(fragCoord / pixelSize);\n" +
"  int x = int(mod(scaledCoord.x, 8.0));\n" +
"  int y = int(mod(scaledCoord.y, 8.0));\n" +
"  float threshold = bayer[y*8+x] - 0.25;\n" +
"  float stp = 1.0 / (colorNum - 1.0);\n" +
"  col += threshold * stp;\n" +
"  float luminance = dot(col, vec3(0.2126, 0.7152, 0.0722));\n" +
"  float bias = mix(0.2, 0.0, smoothstep(0.45, 0.8, luminance));\n" +
"  col = clamp(col - bias, 0.0, 1.0);\n" +
"  col = floor(col * (colorNum - 1.0) + 0.5) / (colorNum - 1.0);\n" +
"  fragColor = vec4(col, 1.0);\n" +
"}\n";

  function compile(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.warn("dither shader:", gl.getShaderInfoLog(s));
      return null;
    }
    return s;
  }

  var vs = compile(gl.VERTEX_SHADER, vertSrc);
  var fs = compile(gl.FRAGMENT_SHADER, fragSrc);
  if (!vs || !fs) return;
  var prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return;
  gl.useProgram(prog);

  // fullscreen quad
  var buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  var loc = gl.getAttribLocation(prog, "position");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  var U = {};
  ["resolution", "time", "waveSpeed", "waveFrequency", "waveAmplitude",
   "waveColor", "backgroundColor", "mousePos", "enableMouseInteraction",
   "mouseRadius", "colorNum", "pixelSize"].forEach(function (n) {
    U[n] = gl.getUniformLocation(prog, n);
  });

  gl.uniform1f(U.waveSpeed, WAVE_SPEED);
  gl.uniform1f(U.waveFrequency, WAVE_FREQUENCY);
  gl.uniform1f(U.waveAmplitude, WAVE_AMPLITUDE);
  gl.uniform3f(U.waveColor, WAVE_COLOR[0], WAVE_COLOR[1], WAVE_COLOR[2]);
  gl.uniform3f(U.backgroundColor, BG_COLOR[0], BG_COLOR[1], BG_COLOR[2]);
  gl.uniform1f(U.mouseRadius, MOUSE_RADIUS);
  gl.uniform1f(U.colorNum, COLOR_NUM);
  gl.uniform1f(U.pixelSize, PIXEL_SIZE);
  gl.uniform1i(U.enableMouseInteraction, 1);

  var mouse = [0, 0];
  window.addEventListener("pointermove", function (e) {
    mouse[0] = e.clientX; mouse[1] = e.clientY;
  }, { passive: true });

  function resize() {
    var w = Math.floor(canvas.clientWidth);
    var h = Math.floor(canvas.clientHeight);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w; canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
    gl.uniform2f(U.resolution, canvas.width, canvas.height);
  }
  window.addEventListener("resize", resize);

  function render(tMs) {
    resize();
    gl.uniform1f(U.time, reduce ? 1.5 : tMs * 0.001);
    gl.uniform2f(U.mousePos, mouse[0], mouse[1]);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    if (!reduce && !document.hidden) requestAnimationFrame(render);
  }
  requestAnimationFrame(render);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !reduce) requestAnimationFrame(render);
  });
})();
