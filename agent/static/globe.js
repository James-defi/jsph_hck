import * as THREE from "three";

const MAP_URL = "https://unpkg.com/three-globe/example/img/earth-blue-marble.jpg";
const WATER_MASK_URL = "https://unpkg.com/three-globe/example/img/earth-water.png";
const PURPLE = 0x6958f8;
const PURPLE_SHADES = [0x5b4cdb, 0x7c3aed, 0x8b5cf6, 0xa78bfa, 0xc4b5fd, 0x6d28d9];
const MODE_CYCLE = {air: 14000, rail: 18000, bus: 16000};
const ZOOM_MIN = 2.4;
const ZOOM_MAX = 7.2;
const ZOOM_DEFAULT = 4.35;

const GEO = {
  VKO: {lat: 55.591, lon: 37.261, label: "VKO"},
  SVO: {lat: 55.973, lon: 37.415, label: "SVO"},
  DME: {lat: 55.414, lon: 37.906, label: "DME"},
  LED: {lat: 59.8, lon: 30.263, label: "LED"},
  SVX: {lat: 56.743, lon: 60.803, label: "SVX"},
  KZN: {lat: 55.606, lon: 49.277, label: "KZN"},
  IST: {lat: 41.275, lon: 28.752, label: "IST"},
  SAW: {lat: 40.898, lon: 29.309, label: "SAW"},
  AYT: {lat: 36.898, lon: 30.8, label: "AYT"},
  LHR: {lat: 51.47, lon: -0.454, label: "LHR"},
  LGW: {lat: 51.153, lon: -0.182, label: "LGW"},
  STN: {lat: 51.885, lon: 0.235, label: "STN"},
  MOW: {lat: 55.756, lon: 37.617, label: "MOW"},
  MSK: {lat: 55.756, lon: 37.617, label: "MSK"},
  SPB: {lat: 59.932, lon: 30.308, label: "SPB"},
  2006004: {lat: 55.756, lon: 37.617, label: "Москва"},
  2004004: {lat: 59.932, lon: 30.308, label: "Санкт-Петербург"},
  2004001: {lat: 59.932, lon: 30.308, label: "Санкт-Петербург"},
  KLF: {lat: 54.514, lon: 36.267, label: "KLF"},
  VBG: {lat: 60.711, lon: 28.749, label: "VBG"},
  AER: {lat: 43.45, lon: 39.956, label: "AER"},
  KRR: {lat: 45.034, lon: 39.17, label: "KRR"},
  ROV: {lat: 47.258, lon: 39.818, label: "ROV"},
  OVB: {lat: 55.012, lon: 82.65, label: "OVB"},
  CEK: {lat: 55.306, lon: 61.503, label: "CEK"},
  UFA: {lat: 54.557, lon: 55.874, label: "UFA"},
  KUF: {lat: 53.505, lon: 50.164, label: "KUF"},
  GOJ: {lat: 56.23, lon: 43.784, label: "GOJ"},
  PEE: {lat: 57.914, lon: 56.021, label: "PEE"},
  TJM: {lat: 57.189, lon: 65.324, label: "TJM"},
  KGD: {lat: 54.89, lon: 20.592, label: "KGD"},
  MRV: {lat: 44.225, lon: 43.082, label: "MRV"},
  SGC: {lat: 61.344, lon: 73.402, label: "SGC"},
  CDG: {lat: 49.01, lon: 2.548, label: "CDG"},
  FRA: {lat: 50.037, lon: 8.562, label: "FRA"},
  AMS: {lat: 52.308, lon: 4.764, label: "AMS"},
  MUC: {lat: 48.354, lon: 11.786, label: "MUC"},
  VIE: {lat: 48.11, lon: 16.57, label: "VIE"},
  WAW: {lat: 52.166, lon: 20.967, label: "WAW"},
  PRG: {lat: 50.101, lon: 14.26, label: "PRG"},
  HEL: {lat: 60.317, lon: 24.963, label: "HEL"},
  RIX: {lat: 56.924, lon: 23.971, label: "RIX"},
  TLL: {lat: 59.413, lon: 24.833, label: "TLL"},
  MSQ: {lat: 53.882, lon: 28.031, label: "MSQ"},
  FCO: {lat: 41.8, lon: 12.239, label: "FCO"},
  BCN: {lat: 41.297, lon: 2.078, label: "BCN"},
  MAD: {lat: 40.472, lon: -3.563, label: "MAD"},
  DXB: {lat: 25.253, lon: 55.365, label: "DXB"},
  AUH: {lat: 24.433, lon: 54.651, label: "AUH"},
  JFK: {lat: 40.641, lon: -73.778, label: "JFK"},
  TBS: {lat: 41.669, lon: 44.955, label: "TBS"},
  EVN: {lat: 40.147, lon: 44.396, label: "EVN"},
  GYD: {lat: 40.467, lon: 50.047, label: "GYD"},
  ALA: {lat: 43.352, lon: 77.04, label: "ALA"},
  TAS: {lat: 41.257, lon: 69.281, label: "TAS"},
  SIP: {lat: 45.052, lon: 33.975, label: "SIP"},
};

const CITY_AIR = {
  москва: "VKO",
  moscow: "VKO",
  петербург: "LED",
  питер: "LED",
  санктпетербург: "LED",
  екатеринбург: "SVX",
  казань: "KZN",
  лондон: "LHR",
  london: "LHR",
  стамбул: "IST",
  istanbul: "IST",
  сочи: "AER",
  новосибирск: "OVB",
};

const CITY_GROUND = {
  москва: "MSK",
  moscow: "MSK",
  петербург: "SPB",
  питер: "SPB",
  санктпетербург: "SPB",
  екатеринбург: "SVX",
  казань: "KZN",
  калуга: "KLF",
  выборг: "VBG",
  лондон: "LHR",
  стамбул: "IST",
};

function latLonToVector3(lat, lon, radius = 1) {
  const phi = THREE.MathUtils.degToRad(90 - lat);
  const theta = THREE.MathUtils.degToRad(lon + 180);
  return new THREE.Vector3(
    -radius * Math.sin(phi) * Math.cos(theta),
    radius * Math.cos(phi),
    radius * Math.sin(phi) * Math.sin(theta),
  );
}

function fold(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/ё/g, "е")
    .replace(/[^a-zа-я0-9]+/g, "");
}

function tokensOf(raw) {
  return String(raw || "")
    .split(/[^0-9a-zа-яё]+/i)
    .map(fold)
    .filter(Boolean);
}

function modeFromLabel(value) {
  const text = String(value || "").toLowerCase();
  if (/(поезд|rail|train|жд|etrain)/.test(text)) return "rail";
  if (/(автобус|bus|coach)/.test(text)) return "bus";
  return "air";
}

function finiteCoord(value) {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function isValidPlace(place) {
  return Boolean(place) && Number.isFinite(place.lat) && Number.isFinite(place.lon);
}

function samePoint(a, b) {
  return a.lat.toFixed(3) === b.lat.toFixed(3) && a.lon.toFixed(3) === b.lon.toFixed(3);
}

function placeId(place) {
  return `${place.lat.toFixed(3)}:${place.lon.toFixed(3)}`;
}

function placeFromGeo(code) {
  const geo = GEO[code];
  if (!geo) return null;
  return {lat: geo.lat, lon: geo.lon, label: geo.label || code, code};
}

function placeFromDict(dict) {
  if (!dict || typeof dict !== "object") return null;
  const lat = finiteCoord(dict.lat);
  const lon = finiteCoord(dict.lon);
  if (lat === null || lon === null) return null;
  const code = String(dict.code || "").trim().toUpperCase();
  const label = String(dict.label || code || "").trim() || `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
  return {lat, lon, label, code};
}

function cityCodeFromAlias(foldedFull, tokens, mode) {
  const table = mode === "air" ? CITY_AIR : CITY_GROUND;
  if (Object.prototype.hasOwnProperty.call(table, foldedFull)) return table[foldedFull];
  for (const token of tokens) {
    if (Object.prototype.hasOwnProperty.call(table, token)) return table[token];
  }
  return "";
}

function lookupPlace(raw, mode) {
  if (!raw) return null;
  const text = String(raw).trim();
  if (!text) return null;
  if (/^\d{4,}$/.test(text) && GEO[text]) return placeFromGeo(text);
  const station = text.match(/\((\d{4,})\)/);
  if (station && GEO[station[1]]) return placeFromGeo(station[1]);
  const iata = text.toUpperCase().match(/\b([A-Z]{3})\b/);
  if (iata && GEO[iata[1]]) return placeFromGeo(iata[1]);
  const folded = fold(text);
  if (GEO[folded.toUpperCase()]) return placeFromGeo(folded.toUpperCase());
  const code = cityCodeFromAlias(folded, tokensOf(text), mode);
  return code ? placeFromGeo(code) : null;
}

function resolvePlace(dict, fallbackRaw, mode) {
  const fromCoords = placeFromDict(dict);
  if (fromCoords) return fromCoords;
  return lookupPlace(dict?.code, mode) || lookupPlace(dict?.label, mode) || lookupPlace(fallbackRaw, mode);
}

function routesFromTimeline(timeline) {
  if (!Array.isArray(timeline) || !timeline.length) return [];
  const seen = new Set();
  const routes = [];
  for (const traveler of timeline) {
    const person = String(traveler.person || "Участник").split("·")[0].trim();
    for (const leg of traveler.legs || []) {
      const mode = modeFromLabel(leg.mode);
      const raw = String(leg.route || "");
      const parts = raw.split(/\s*(?:→|->|—|–)\s*/).map((part) => part.trim());
      const fromPlace = resolvePlace(leg.origin, parts[0], mode);
      const toPlace = resolvePlace(leg.destination, parts[1], mode);
      if (!fromPlace || !toPlace || samePoint(fromPlace, toPlace)) continue;
      const isCommon = /общ/i.test(raw);
      const key = `${placeId(fromPlace)}|${placeId(toPlace)}|${mode}|${isCommon ? "c" : "f"}`;
      if (seen.has(key)) continue;
      seen.add(key);
      routes.push({
        from: fromPlace,
        to: toPlace,
        mode,
        phase: isCommon ? "common" : "feeder",
        person: isCommon ? "Группа" : person,
      });
    }
  }
  return routes;
}

function makeIconTexture(kind) {
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, 128, 128);
  ctx.translate(64, 64);
  ctx.fillStyle = "#ffffff";
  ctx.strokeStyle = "#ffffff";
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  if (kind === "rail") {
    ctx.lineWidth = 5;
    ctx.strokeRect(-34, -16, 68, 28);
    ctx.fillRect(-34, -16, 18, 28);
    for (const x of [-8, 10, 28]) ctx.fillRect(x - 6, -8, 10, 12);
    ctx.beginPath();
    ctx.arc(-18, 18, 6, 0, Math.PI * 2);
    ctx.arc(18, 18, 6, 0, Math.PI * 2);
    ctx.fill();
  } else if (kind === "bus") {
    ctx.lineWidth = 5;
    ctx.strokeRect(-36, -14, 72, 26);
    ctx.fillRect(-36, -14, 14, 26);
    for (const x of [-8, 10, 28]) ctx.fillRect(x - 6, -6, 10, 10);
    ctx.fillRect(-28, 14, 10, 6);
    ctx.fillRect(18, 14, 10, 6);
  } else {
    ctx.beginPath();
    ctx.moveTo(0, -38);
    ctx.lineTo(-10, 4);
    ctx.lineTo(-40, 16);
    ctx.lineTo(-36, 24);
    ctx.lineTo(-8, 14);
    ctx.lineTo(-4, 36);
    ctx.lineTo(-14, 46);
    ctx.lineTo(0, 38);
    ctx.lineTo(14, 46);
    ctx.lineTo(4, 36);
    ctx.lineTo(8, 14);
    ctx.lineTo(36, 24);
    ctx.lineTo(40, 16);
    ctx.lineTo(10, 4);
    ctx.closePath();
    ctx.fill();
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function slerpOnSphere(start, end, t) {
  const point = new THREE.Vector3();
  const dot = Math.min(Math.max(start.dot(end), -1), 1);
  const theta = Math.acos(dot);
  if (!Number.isFinite(theta) || theta < 1e-5) return point.copy(start);
  const sin = Math.sin(theta);
  point.copy(start).multiplyScalar(Math.sin((1 - t) * theta) / sin);
  point.addScaledVector(end, Math.sin(t * theta) / sin);
  return point;
}

function routePoints(from, to, mode, samples = 96) {
  const start = latLonToVector3(from.lat, from.lon, 1);
  const end = latLonToVector3(to.lat, to.lon, 1);
  const side = start.clone().cross(end);
  if (side.lengthSq() < 1e-10) {
    side.crossVectors(start, new THREE.Vector3(0, 1, 0));
    if (side.lengthSq() < 1e-10) side.crossVectors(start, new THREE.Vector3(1, 0, 0));
  }
  side.normalize();
  const ground = mode === "rail" || mode === "bus";
  // A rail or bus line bends slightly around terrain but never leaves the
  // surface: only aviation gets the lifted arc.
  const wander = ground ? Math.min(0.03, start.angleTo(end) * 0.06) : 0;
  const points = [];
  for (let i = 0; i <= samples; i += 1) {
    const t = i / samples;
    const point = slerpOnSphere(start, end, t).normalize();
    if (wander) point.addScaledVector(side, Math.sin(Math.PI * t) * wander).normalize();
    const lift = ground ? 0.006 : Math.sin(Math.PI * t) * 0.3;
    point.multiplyScalar(1 + lift);
    points.push(point);
  }
  return points;
}

function createRouteLine(points, color = PURPLE, ground = false) {
  const curve = new THREE.CatmullRomCurve3(points);
  const geometry = new THREE.TubeGeometry(curve, ground ? 80 : 64, ground ? 0.0045 : 0.006, 8, false);
  const material = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: ground ? 0.92 : 0.95,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.renderOrder = 1;
  return mesh;
}

function pointOnArc(points, t) {
  const clamped = Math.min(Math.max(t, 0), 0.999);
  const scaled = clamped * (points.length - 1);
  const index = Math.floor(scaled);
  const next = Math.min(index + 1, points.length - 1);
  const mix = scaled - index;
  return {
    position: points[index].clone().lerp(points[next], mix),
    tangent: points[next].clone().sub(points[index]).normalize(),
  };
}

function readBootstrap() {
  const node = document.getElementById("globe-bootstrap");
  if (!node) return {timeline: []};
  try {
    return JSON.parse(node.textContent || "{}");
  } catch {
    return {timeline: []};
  }
}

export function mountGlobe(stage) {
  if (!stage) return;

  const webglCanvas = stage.querySelector("#webgl-canvas");
  const labelCanvas = stage.querySelector("#label-canvas");
  if (!webglCanvas) return;

  const ctx2d = labelCanvas ? labelCanvas.getContext("2d") : null;
  let width = stage.clientWidth || 1;
  let height = stage.clientHeight || 1;

  const renderer = new THREE.WebGLRenderer({canvas: webglCanvas, antialias: true, alpha: false});
  renderer.setSize(width, height);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x07060d, 1);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 100);
  camera.position.set(0, 0, ZOOM_DEFAULT);

  const textureLoader = new THREE.TextureLoader();
  const geometry = new THREE.SphereGeometry(1, 64, 64);
  const earthGroup = new THREE.Group();
  const routeGroup = new THREE.Group();
  earthGroup.add(routeGroup);
  scene.add(earthGroup);
  scene.add(new THREE.AmbientLight(0xffffff, 0.28));
  const dirLight = new THREE.DirectionalLight(0xffffff, 1.35);
  dirLight.position.set(5, 3, 5);
  scene.add(dirLight);

  const iconTextures = {
    air: makeIconTexture("air"),
    rail: makeIconTexture("rail"),
    bus: makeIconTexture("bus"),
  };

  let routeState = [];
  let earthReady = false;
  let pendingTimeline = readBootstrap().timeline || [];
  if (Array.isArray(window.__jarvelPendingTimeline)) {
    pendingTimeline = window.__jarvelPendingTimeline;
  }
  let isDragging = false;
  const lastBall = new THREE.Vector3();
  const nextBall = new THREE.Vector3();
  const dragAxis = new THREE.Vector3();

  function applyTimeline(timeline) {
    pendingTimeline = Array.isArray(timeline) ? timeline : [];
    window.__jarvelPendingTimeline = pendingTimeline;
    rebuildRoutes(routesFromTimeline(pendingTimeline));
  }

  function startEarth(diffuseMap, waterMask) {
    if (earthReady) return;
    if (diffuseMap) {
      earthGroup.add(new THREE.Mesh(geometry, new THREE.MeshPhongMaterial({
        map: diffuseMap,
        specularMap: waterMask || null,
        specular: new THREE.Color(0x222222),
        shininess: 12,
      })));
    } else {
      earthGroup.add(new THREE.Mesh(geometry, new THREE.MeshPhongMaterial({
        color: 0x1c3f73,
        emissive: 0x081018,
        shininess: 8,
      })));
    }
    earthReady = true;
    applyTimeline(pendingTimeline);
    animate();
  }

  textureLoader.load(
    MAP_URL,
    (diffuseMap) => {
      textureLoader.load(
        WATER_MASK_URL,
        (waterMask) => startEarth(diffuseMap, waterMask),
        undefined,
        () => startEarth(diffuseMap, null),
      );
    },
    undefined,
    () => startEarth(null, null),
  );

  function clearRoutes() {
    while (routeGroup.children.length) {
      const child = routeGroup.children[0];
      routeGroup.remove(child);
      if (child.isSprite) {
        child.material?.dispose();
        continue;
      }
      child.geometry?.dispose();
      child.material?.dispose();
    }
    routeState = [];
  }

  function clampZoom(value) {
    return THREE.MathUtils.clamp(value, ZOOM_MIN, ZOOM_MAX);
  }

  function lookAtPoint(point) {
    const from = point.clone().normalize();
    if (!from.lengthSq()) return;
    const yaw = -Math.atan2(from.x, from.z);
    const pitch = Math.asin(THREE.MathUtils.clamp(from.y, -1, 1));
    earthGroup.quaternion.setFromEuler(new THREE.Euler(pitch, yaw, 0, "YXZ"));
  }

  function orientToRoute(routes) {
    const places = [];
    for (const route of routes) {
      if (isValidPlace(route.from)) places.push(route.from);
      if (isValidPlace(route.to)) places.push(route.to);
    }
    if (!places.length) return;
    const sum = new THREE.Vector3();
    for (const place of places) sum.add(latLonToVector3(place.lat, place.lon, 1));
    if (!sum.lengthSq()) return;
    lookAtPoint(sum.normalize());
  }

  function rebuildRoutes(routes) {
    clearRoutes();
    const safeRoutes = Array.isArray(routes) ? routes : [];
    const places = new Map();
    for (const [index, route] of safeRoutes.entries()) {
      const origin = route.from;
      const destination = route.to;
      if (!isValidPlace(origin) || !isValidPlace(destination)) continue;
      places.set(placeId(origin), origin);
      places.set(placeId(destination), destination);
      const tint = PURPLE_SHADES[index % PURPLE_SHADES.length];
      const ground = route.mode === "rail" || route.mode === "bus";
      const points = routePoints(origin, destination, route.mode);
      routeGroup.add(createRouteLine(points, tint, ground));
      const sprite = new THREE.Sprite(
        new THREE.SpriteMaterial({
          map: iconTextures[route.mode] || iconTextures.air,
          color: new THREE.Color(tint),
          transparent: true,
          depthTest: false,
          depthWrite: false,
        }),
      );
      const iconSize = route.mode === "air" ? 0.28 : 0.15;
      sprite.scale.set(iconSize, iconSize, 1);
      sprite.renderOrder = 3;
      routeGroup.add(sprite);
      routeState.push({...route, points, sprite, origin, destination, iconSize});
    }
    for (const [index, place] of [...places.values()].entries()) {
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(0.028, 12, 12),
        new THREE.MeshBasicMaterial({color: PURPLE_SHADES[index % PURPLE_SHADES.length]}),
      );
      marker.position.copy(latLonToVector3(place.lat, place.lon, 1.012));
      routeGroup.add(marker);
    }
    orientToRoute(safeRoutes);
    if (!safeRoutes.length) {
      lookAtPoint(latLonToVector3(48.2, 21.5, 1));
    }
  }

  function orientSprite(sprite, tangent) {
    if (!tangent.lengthSq()) return;
    const here = sprite.position.clone();
    const ahead = here.clone().add(tangent);
    earthGroup.localToWorld(here);
    earthGroup.localToWorld(ahead);
    here.project(camera);
    ahead.project(camera);
    const angle = Math.atan2(ahead.y - here.y, ahead.x - here.x) - Math.PI / 2;
    if (Number.isFinite(angle)) sprite.material.rotation = angle;
  }

  function projectOnTrackball(clientX, clientY, target) {
    const rect = stage.getBoundingClientRect();
    const nx = ((clientX - rect.left) / Math.max(rect.width, 1)) * 2 - 1;
    const ny = -(((clientY - rect.top) / Math.max(rect.height, 1)) * 2 - 1);
    const globeNdc = 1 / (camera.position.z * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2));
    const aspect = width / Math.max(height, 1);
    target.set((nx * aspect) / globeNdc, ny / globeNdc, 0);
    const radiusSq = target.x * target.x + target.y * target.y;
    if (radiusSq <= 1) target.z = Math.sqrt(1 - radiusSq);
    else target.setLength(1);
    return target;
  }

  stage.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    if (event.target.closest("button, a, textarea, input, label, .chat-panel, .chat-resizer")) return;
    isDragging = true;
    projectOnTrackball(event.clientX, event.clientY, lastBall);
    stage.setPointerCapture?.(event.pointerId);
  });
  stage.addEventListener("pointerup", (event) => {
    isDragging = false;
    if (stage.hasPointerCapture?.(event.pointerId)) {
      stage.releasePointerCapture(event.pointerId);
    }
  });
  stage.addEventListener("pointercancel", () => {
    isDragging = false;
  });
  stage.addEventListener("pointermove", (event) => {
    if (!isDragging) return;
    projectOnTrackball(event.clientX, event.clientY, nextBall);
    dragAxis.copy(lastBall).cross(nextBall);
    if (dragAxis.lengthSq() > 1e-10) {
      const angle = lastBall.angleTo(nextBall);
      if (angle > 1e-6) earthGroup.rotateOnWorldAxis(dragAxis.normalize(), angle);
    }
    lastBall.copy(nextBall);
  });
  stage.addEventListener("wheel", (event) => {
    event.preventDefault();
    const next = camera.position.z * (event.deltaY > 0 ? 1.09 : 0.91);
    camera.position.z = clampZoom(next);
  }, {passive: false});

  function resize() {
    width = stage.clientWidth || 1;
    height = stage.clientHeight || 1;
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    if (labelCanvas && ctx2d) {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      labelCanvas.width = Math.max(1, Math.round(width * dpr));
      labelCanvas.height = Math.max(1, Math.round(height * dpr));
      ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
  }
  window.addEventListener("resize", resize);
  if (typeof ResizeObserver === "function") new ResizeObserver(resize).observe(stage);
  resize();

  function isFacingCamera(localPosition, minDot = 0) {
    const world = localPosition.clone();
    earthGroup.localToWorld(world);
    if (!Number.isFinite(world.x) || !world.lengthSq()) return false;
    const toCamera = camera.position.clone().sub(world);
    if (!toCamera.lengthSq()) return false;
    return world.normalize().dot(toCamera.normalize()) > minDot;
  }

  function projectPoint(vector) {
    const world = vector.clone();
    earthGroup.localToWorld(world);
    const projected = world.clone().project(camera);
    return {
      x: (projected.x * 0.5 + 0.5) * width,
      y: (-projected.y * 0.5 + 0.5) * height,
      front: isFacingCamera(vector, 0.12),
    };
  }

  function animate() {
    requestAnimationFrame(animate);
    if (!earthReady) return;

    const now = performance.now();
    for (const [index, route] of routeState.entries()) {
      const cycle = MODE_CYCLE[route.mode] || MODE_CYCLE.air;
      const local = ((now / cycle) + index * 0.17) % 1;
      const sample = pointOnArc(route.points, local);
      const zoomScale = camera.position.z / ZOOM_DEFAULT;
      const size = (route.iconSize || 0.28) * zoomScale;
      route.sprite.position.copy(sample.position);
      route.sprite.visible = route.mode === "air" || isFacingCamera(sample.position, 0.04);
      if (route.sprite.visible) {
        route.sprite.scale.set(size, size, 1);
        orientSprite(route.sprite, sample.tangent);
      }
    }

    renderer.render(scene, camera);
    if (!ctx2d || !labelCanvas) return;
    ctx2d.clearRect(0, 0, width, height);
    ctx2d.font = "600 13px Inter, ui-sans-serif, sans-serif";
    ctx2d.textBaseline = "middle";
    ctx2d.lineJoin = "round";
    ctx2d.lineWidth = 4;
    ctx2d.strokeStyle = "rgba(8, 10, 18, 0.88)";
    ctx2d.fillStyle = "#f4f7fb";
    const drawn = new Set();
    for (const route of routeState) {
      for (const place of [route.origin, route.destination]) {
        if (drawn.has(place.label)) continue;
        const projected = projectPoint(latLonToVector3(place.lat, place.lon, 1.03));
        if (!projected.front) continue;
        drawn.add(place.label);
        const textX = projected.x + 10;
        const textY = projected.y;
        ctx2d.strokeText(place.label, textX, textY);
        ctx2d.fillText(place.label, textX, textY);
      }
    }
  }

  window.JarvelGlobe = {
    setTimeline(timeline) {
      applyTimeline(timeline);
    },
  };
  if (Array.isArray(window.__jarvelPendingTimeline) && window.__jarvelPendingTimeline.length) {
    applyTimeline(window.__jarvelPendingTimeline);
  }

  window.setTimeout(() => {
    if (!earthReady) startEarth(null, null);
  }, 4000);
}

const stage = document.getElementById("globe-stage");
if (stage) mountGlobe(stage);
