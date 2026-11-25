// 3D object card (jfollow10d). data.json = {slug, view}. view "model": the app serves the card's own
// object.glb (checked natively and by the gateway); three.js parses it here with a manager that
// refuses every URL that is not a blob:/data: of the file itself. view "preview": the ready card's
// picture. Text goes in with textContent only; any failure is one plain line, never a crash.
// A GLB with clips plays them (j3d-anim): Play/Pause or Space toggles, reduced motion starts
// paused, and the camera frames the box the whole motion sweeps, not just the first pose.
import * as THREE from "./vendor/three/build/three.module.min.js";
import { GLTFLoader } from "./vendor/three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "./vendor/three/examples/jsm/controls/OrbitControls.js";

const MAX_GLB = 50 * 1024 * 1024;
const PAGE = 0x141414; // model.css --page: the canvas clears to the page colour
const GROUND = 0x3a3e45;
const FAIL_TEXT = "Couldn't open this 3D object.";
const stage = document.getElementById("stage");
const status = document.getElementById("status");
const preview = document.getElementById("preview");
const hint = document.getElementById("hint");
const resetButton = document.getElementById("reset");
const playButton = document.getElementById("play");
let refused = 0;
let pinged = false;

// One status ping for the app's web_card row: a fixed word and a count, nothing else.
function ping(error) {
  if (pinged) return;
  pinged = true;
  fetch(`model-status/${error}/${Math.min(refused, 9999)}`).catch(() => {});
}

function fail(error) {
  status.textContent = FAIL_TEXT;
  status.hidden = false;
  ping(error);
}

function showPreview(slug) {
  preview.addEventListener("load", () => {
    status.hidden = true;
    ping("none");
  });
  preview.addEventListener("error", () => fail("fetch"));
  preview.hidden = false;
  preview.src = preview.dataset.src;
  hint.textContent = `Say: pull up the ${slug.replace(/-/g, " ")}.`;
  hint.hidden = false;
}

function makeRenderer() {
  try {
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setClearColor(PAGE);
    return renderer;
  } catch {
    return null;
  }
}

function parse(buffer) {
  const manager = new THREE.LoadingManager();
  // A GLB's own images arrive as blob: URLs; anything else (a file or web address) is refused.
  manager.setURLModifier((url) => (/^(blob|data):/.test(url) ? url : (refused++, "data:,")));
  return new Promise((resolve, reject) => new GLTFLoader(manager).parse(buffer, "", resolve, reject));
}

function frame(scene, object, camera, controls, light, box = new THREE.Box3().setFromObject(object)) {
  if (box.isEmpty()) throw new Error("empty");
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const radius = Math.max(sphere.radius, 1e-3);
  const distance = (radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2))) * 1.15;
  camera.near = distance / 100;
  camera.far = distance * 100;
  camera.position.copy(sphere.center).add(new THREE.Vector3(1, 0.6, 1).normalize().multiplyScalar(distance));
  camera.updateProjectionMatrix();
  controls.target.copy(sphere.center);
  controls.minDistance = radius * 0.4;
  controls.maxDistance = distance * 4;
  controls.update();
  controls.saveState();

  // A lit floor disc a shade above the page, so the shadow reads as a darker patch (a bare
  // ShadowMaterial on the dark canvas was invisible: Mac live run, criterion 37).
  const floor = new THREE.MeshStandardMaterial({ color: GROUND, roughness: 1, metalness: 0 });
  const ground = new THREE.Mesh(new THREE.CircleGeometry(radius * 3, 64), floor);
  ground.rotation.x = -Math.PI / 2;
  ground.position.set(sphere.center.x, box.min.y, sphere.center.z);
  ground.receiveShadow = true;
  scene.add(ground);

  // From back-left of the camera, so the shadow falls to the right of the object, in view.
  light.position.copy(sphere.center).add(new THREE.Vector3(-radius * 1.5, radius * 4, radius * 2));
  light.target.position.copy(sphere.center);
  scene.add(light.target);
  const cam = light.shadow.camera;
  cam.left = cam.bottom = -radius * 2;
  cam.right = cam.top = radius * 2;
  cam.near = radius * 0.1;
  cam.far = radius * 10;
  cam.updateProjectionMatrix();
}

// The camera has to frame the space every pose of every clip reaches, not just the loaded
// pose: sample the longest clip at 8 even times (both ends) and union the boxes. The final
// setTime(0) leaves the nodes on the t=0 pose, the one a paused page shows. The floor then sits at
// the lowest point of the whole motion on purpose: that is where a dropped or bouncing object lands,
// so a raised first pose reads as mid-air with its shadow below, not as an object sunk into the floor.
function wholeClipBox(mixer, clips, object) {
  const box = new THREE.Box3();
  const longest = Math.max(...clips.map((clip) => clip.duration));
  for (let i = 0; i <= 7; i++) {
    mixer.setTime((longest * i) / 7);
    object.updateMatrixWorld(true);
    box.union(new THREE.Box3().setFromObject(object));
  }
  mixer.setTime(0);
  return box;
}

async function showModel() {
  let buffer;
  try {
    const res = await fetch("object.glb");
    if (!res.ok) return fail("fetch");
    buffer = await res.arrayBuffer();
  } catch {
    return fail("fetch");
  }
  if (buffer.byteLength > MAX_GLB) return fail("size");
  const renderer = makeRenderer();
  if (!renderer) return fail("webgl");

  let gltf;
  try {
    gltf = await parse(buffer);
  } catch {
    return fail("parse");
  }
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 1000);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x404048, 1.6));
  const light = new THREE.DirectionalLight(0xffffff, 2.2);
  light.castShadow = true;
  light.shadow.mapSize.set(1024, 1024);
  scene.add(light);
  gltf.scene.traverse((node) => {
    if (node.isMesh) node.castShadow = true;
  });
  scene.add(gltf.scene);
  const clips = gltf.animations || [];
  const mixer = clips.length > 0 ? new THREE.AnimationMixer(gltf.scene) : null;
  if (mixer) {
    for (const clip of clips) mixer.clipAction(clip).setLoop(THREE.LoopRepeat, Infinity).play();
  }

  stage.prepend(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.screenSpacePanning = true;
  try {
    frame(scene, gltf.scene, camera, controls, light, mixer ? wholeClipBox(mixer, clips, gltf.scene) : undefined);
  } catch {
    renderer.domElement.remove();
    return fail("parse");
  }

  let queued = false;
  const render = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      renderer.render(scene, camera);
    });
  };
  controls.addEventListener("change", render);

  const fit = () => {
    const w = Math.max(stage.clientWidth, 1);
    const h = Math.max(stage.clientHeight, 1);
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    render();
  };
  new ResizeObserver(fit).observe(stage);
  fit();

  // WebKit sends a trackpad pinch as gesture events: map it to the same dolly as the wheel.
  let pinchStart = 0;
  stage.addEventListener("gesturestart", (e) => {
    e.preventDefault();
    pinchStart = camera.position.distanceTo(controls.target);
  });
  stage.addEventListener("gesturechange", (e) => {
    e.preventDefault();
    if (!pinchStart || !e.scale) return;
    const d = THREE.MathUtils.clamp(pinchStart / e.scale, controls.minDistance, controls.maxDistance);
    camera.position.sub(controls.target).setLength(d).add(controls.target);
    controls.update();
  });
  stage.addEventListener("gestureend", (e) => e.preventDefault());

  resetButton.addEventListener("click", () => {
    controls.reset();
    render();
  });

  // Clip playback (j3d-anim): one rAF chain, alive only while playing and the tab visible; the
  // dt cap stops a hidden tab's time jump from teleporting the pose. Reduced motion starts
  // paused at t=0. A static GLB shows no button, no loop, today's behaviour untouched.
  stage.dataset.clips = String(clips.length);
  if (mixer) {
    let playing = false;
    let raf = 0;
    let last = 0;
    const sync = () => {
      stage.dataset.anim = playing ? "playing" : "paused";
      playButton.textContent = playing ? "Pause" : "Play"; // the label is the state: no aria-pressed too
    };
    const tick = () => {
      if (!playing || document.hidden) {
        raf = 0;
        return;
      }
      const now = performance.now();
      mixer.update(Math.min((now - last) / 1000, 0.1));
      last = now;
      renderer.render(scene, camera);
      raf = requestAnimationFrame(tick);
    };
    const setPlaying = (value) => {
      playing = value;
      if (value) {
        last = performance.now();
        if (!raf) raf = requestAnimationFrame(tick);
      } else if (raf) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
      sync();
    };
    const toggle = () => setPlaying(!playing);
    playButton.addEventListener("click", toggle);
    stage.addEventListener("keydown", (e) => {
      if (e.code !== "Space") return;
      e.preventDefault();
      if (!e.repeat) toggle();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
      } else if (playing && !raf) {
        last = performance.now();
        raf = requestAnimationFrame(tick);
      }
    });
    stage.setAttribute("aria-label", `${stage.getAttribute("aria-label")} Space to play or pause.`);
    playButton.hidden = false;
    const reduce = matchMedia("(prefers-reduced-motion: reduce)");
    reduce.addEventListener("change", () => {
      if (reduce.matches) setPlaying(false); // turned on while the card is open: stop the motion
    });
    setPlaying(!reduce.matches);
  } else {
    stage.dataset.anim = "none";
  }
  resetButton.hidden = false;
  status.hidden = true;
  ping("none");
}

async function main() {
  let data;
  try {
    const res = await fetch("data.json");
    data = await res.json();
  } catch {
    return fail("fetch");
  }
  if (!data || typeof data.slug !== "string") return fail("fetch");
  if (data.view === "preview") return showPreview(data.slug);
  return showModel();
}

main().catch(() => fail("parse"));
