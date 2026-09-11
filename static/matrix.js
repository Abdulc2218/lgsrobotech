/* Matrix-style digital rain background for Robotech 6.0 */
(function () {
  var canvas = document.getElementById("matrix-bg");
  if (!canvas) return;
  var ctx = canvas.getContext("2d");
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var GLYPHS = "0123456789";
  var FONT_SIZE = 16;
  var FRAME_MS = 50;            // ~20 fps: the classic slow drip
  var PURPLE = "rgba(139, 124, 247, 0.55)";
  var GREEN = "rgba(126, 211, 33, 0.6)";
  var FADE = "rgba(13, 10, 31, 0.10)";
  var BG = "#0d0a1f";

  var columns = 0;
  var drops = [];               // current row of each column's falling head
  var colors = [];

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    columns = Math.floor(canvas.width / FONT_SIZE) + 1;
    drops = [];
    colors = [];
    for (var i = 0; i < columns; i++) {
      drops[i] = Math.floor(Math.random() * (canvas.height / FONT_SIZE));
      colors[i] = Math.random() < 0.12 ? GREEN : PURPLE;
    }
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }

  function draw() {
    // translucent wipe leaves fading trails behind each head
    ctx.fillStyle = FADE;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.font = FONT_SIZE + "px monospace";
    for (var i = 0; i < columns; i++) {
      var ch = GLYPHS.charAt(Math.floor(Math.random() * GLYPHS.length));
      ctx.fillStyle = colors[i];
      ctx.fillText(ch, i * FONT_SIZE, drops[i] * FONT_SIZE);
      if (drops[i] * FONT_SIZE > canvas.height && Math.random() > 0.975) {
        drops[i] = 0;           // restart column at the top, staggered
        colors[i] = Math.random() < 0.12 ? GREEN : PURPLE;
      }
      drops[i]++;
    }
  }

  var last = 0;
  function loop(t) {
    if (t - last > FRAME_MS) {
      draw();
      last = t;
    }
    requestAnimationFrame(loop);
  }

  window.addEventListener("resize", resize);
  resize();
  if (reduced) {
    for (var i = 0; i < 40; i++) draw();   // static rain, no animation
  } else {
    requestAnimationFrame(loop);
  }
})();
