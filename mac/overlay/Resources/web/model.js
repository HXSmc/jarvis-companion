// 3D object card (jfollow10d). data.json = {slug, view}. view "model": the app serves the card's own
// object.glb (checked natively and by the gateway); three.js parses it here with a manager that
// refuses every URL that is not a blob:/data: of the file itself. view "preview": the ready card's
// picture. Text goes in with textContent only; any failure is one plain line, never a crash.
import * as THREE from "./vendor/three/build/three.module.min.js";
import { GLTFLoader } from "./vendor/three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "./vendor/three/examples/jsm/controls/OrbitControls.js";

const MAX_GLB = 50 * 1024 * 1024;
const FAIL_TEXT = "Couldn't open this 3D object.";
const stage = document.getElementById("stage");
const status = document.getElementById("status");
const preview = document.getElementById("preview");
const hint = document.getElementById("hint");
const resetButton = document.getElementById("reset");
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

function frame(scene, object, camera, controls, light) {
  const box = new THREE.Box3().setFromObject(object);
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

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(radius * 8, radius * 8), new THREE.ShadowMaterial({ opacity: 0.3 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.set(sphere.center.x, box.min.y, sphere.center.z);
  ground.receiveShadow = true;
  scene.add(ground);

  light.position.copy(sphere.center).add(new THREE.Vector3(radius * 2, radius * 4, radius * 1.5));
  light.target.position.copy(sphere.center);
  scene.add(light.target);
  const cam = light.shadow.camera;
  cam.left = cam.bottom = -radius * 2;
  cam.right = cam.top = radius * 2;
  cam.near = radius * 0.1;
  cam.far = radius * 10;
  cam.updateProjectionMatrix();
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

  stage.prepend(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.screenSpacePanning = true;
  try {
    frame(scene, gltf.scene, camera, controls, light);
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
