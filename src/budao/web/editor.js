'use strict';
const $ = id => document.getElementById(id);
const map = L.map('map').setView([0, 0], 17);
const layers = L.layerGroup().addTo(map);
const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
});
let config = {coordinate_system:'WGS84',region:[],corridors:[],baseline:[],speed_mps:2.5,minimum_distance_m:1500,margin_m:50,snap_tolerance_m:5};
let mode = null, draft = [];
const xy = points => points.map(p => [p.lat, p.lon]);
const point = (lon, lat) => ({lon,lat});
const same = (a,b) => a.lon === b.lon && a.lat === b.lat;
function status(text, error=false) { $('status').textContent=text; $('status').classList.toggle('error',error); }
function parameters() { config.speed_mps=Number($('speed').value);config.minimum_distance_m=Number($('minimum').value); }
function redraw(fit=false) {
  layers.clearLayers();
  if(config.region.length) L.polygon(xy(config.region),{color:'#3a9b68',weight:2,fillOpacity:.09}).addTo(layers);
  for(const road of config.corridors) L.polyline(xy(road),{color:'#76877d',weight:5,opacity:.65}).addTo(layers);
  if(config.baseline.length) L.polyline(xy(config.baseline),{color:'#286bca',weight:3}).addTo(layers);
  if(draft.length) { L.polyline(xy(draft),{color:'#d98b31',dashArray:'4 5'}).addTo(layers);for(const p of draft)L.circleMarker([p.lat,p.lon],{radius:4,color:'#d98b31'}).addTo(layers); }
  parameters();$('json').value=JSON.stringify(config,null,2);
  const points=[...config.region,...config.baseline,...config.corridors.flat()];
  if(fit&&points.length>1)map.fitBounds(L.latLngBounds(xy(points)),{padding:[30,30],maxZoom:18});
}
function load(value) {
  if(value.coordinate_system !== 'WGS84')throw Error('配置必须明确为WGS84');
  if(!Array.isArray(value.region)||!Array.isArray(value.corridors)||!Array.isArray(value.baseline))throw Error('缺少区域、通道或基线');
  config=value;draft=[];mode=null;$('speed').value=config.speed_mps;$('minimum').value=config.minimum_distance_m;redraw(true);
  status('已载入；请校验。本工具不证明App认可该路线。');
}
$('tiles').onchange=()=> $('tiles').checked ? tiles.addTo(map) : map.removeLayer(tiles);
$('demo').onclick=()=> {
  const p=(x,y)=>point(x/111319.490793,y/110574.275822);
  load({coordinate_system:'WGS84',region:[p(-20,-20),p(220,-20),p(220,220),p(-20,220),p(-20,-20)],
    corridors:[...[0,50,100,150,200].map(y=>[p(0,y),p(50,y),p(100,y),p(150,y),p(200,y)]),...[0,50,100,150,200].map(x=>[p(x,0),p(x,50),p(x,100),p(x,150),p(x,200)])],
    baseline:[p(0,0),p(200,0),p(200,200),p(0,200),p(0,0)],speed_mps:2.5,minimum_distance_m:1500,margin_m:50,snap_tolerance_m:5});
  status('合成演示地图，不是校园区域；不能用于正式跑步。');
};
for(const button of document.querySelectorAll('[data-mode]'))button.onclick=()=> {
  mode=button.dataset.mode;draft=[];
  for(const b of document.querySelectorAll('[data-mode]'))b.classList.toggle('active',b===button);
  $('modeHint').textContent=`正在绘制${button.textContent.slice(2)}：点击地图加点，再点“完成当前线”。`;redraw();
};
map.on('click',event=> {if(mode){draft.push(point(event.latlng.lng,event.latlng.lat));redraw();}});
$('undo').onclick=()=>{draft.pop();redraw();};
$('finish').onclick=()=> {
  const minimum=mode==='region'?3:2;
  if(!mode||draft.length<minimum){status(`当前绘制至少需要${minimum}个点`,true);return;}
  if(mode==='region'){config.region=[...draft,draft[0]];}else if(mode==='corridor'){config.corridors.push([...draft]);}else{config.baseline=[...draft];}
  draft=[];mode=null;for(const b of document.querySelectorAll('[data-mode]'))b.classList.remove('active');redraw();status('绘制已完成，尚未校验。');
};
$('closeBaseline').onclick=()=> {const p=config.baseline;if(p.length>1&&!same(p[0],p[p.length-1]))p.push({...p[0]});redraw();};
$('addRoad').onclick=()=> {if(config.baseline.length<2){status('没有基线',true);return;}config.corridors.push(config.baseline.map(p=>({...p})));redraw();status('已将基线认定为可跑通道；请核对实际道路。');};
$('apply').onclick=()=>{try{load(JSON.parse($('json').value));}catch(error){status(error.message,true);}};
for(const id of ['speed','minimum'])$(id).onchange=()=>redraw();
$('file').onchange=async()=> {
  try {
    const file=$('file').files[0];if(!file)return;if(file.size>2*1024*1024)throw Error('文件不能超过2MiB');
    const text=await file.text();let points;
    if(/\.(json|geojson)$/i.test(file.name)) {
      const value=JSON.parse(text);if(value.region){load(value);return;}
      const geometry=value.geometry||value;
      if(value.crs||(value.coordinate_system&&value.coordinate_system!=='WGS84')||geometry.type!=='LineString')throw Error('只接收WGS84单条GeoJSON LineString');
      points=geometry.coordinates.map(p=>point(p[0],p[1]));
    } else {
      if(/<!DOCTYPE|<!ENTITY/i.test(text))throw Error('不接受DTD/实体');
      const xml=new DOMParser().parseFromString(text,'application/xml');if(xml.querySelector('parsererror'))throw Error('XML解析失败');
      if(/\.kml$/i.test(file.name)) {
        const lines=xml.getElementsByTagNameNS('*','coordinates');if(lines.length!==1)throw Error('KML必须只有一条轨迹');
        points=lines[0].textContent.trim().split(/\s+/).map(p=>{const v=p.split(',').map(Number);return point(v[0],v[1]);});
      } else if(/\.gpx$/i.test(file.name)) {
        if(xml.getElementsByTagNameNS('*','trkseg').length>1)throw Error('GPX多段轨迹不能自动拼接');
        let nodes=[...xml.getElementsByTagNameNS('*','trkpt')];if(!nodes.length)nodes=[...xml.getElementsByTagNameNS('*','rtept')];
        points=nodes.map(p=>point(Number(p.getAttribute('lon')),Number(p.getAttribute('lat'))));
      } else throw Error('仅接收配置JSON、KML、GPX、GeoJSON');
    }
    if(points.length<2||points.some(p=>!Number.isFinite(p.lon)||!Number.isFinite(p.lat)||Math.abs(p.lon)>180||Math.abs(p.lat)>85))throw Error('轨迹坐标无效');
    config.baseline=points;config.region=[];config.corridors=[];draft=[];mode=null;redraw(true);
    status('已导入基线。请核对坐标来源，绘制允许区域并添加已知通道，再校验。');
  }catch(error){status(error.message,true);}
};
async function send(action) {
  try {
    if(draft.length)throw Error('请先完成当前绘制');parameters();
    const response=await fetch(`/api/${action}`,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':document.querySelector('meta[name="local-token"]').content},body:JSON.stringify(config)});
    const result=await response.json();if(!response.ok)throw Error(result.error);
    status(`校验通过 · 基线 ${result.baseline_metres} m · ${result.closed_baseline?'闭合':'未闭合'}\n${result.saved_local_path||'尚未保存；不代表真实有效跑步。'}`);
  }catch(error){status(error.message,true);}
}
$('validate').onclick=()=>send('validate');$('save').onclick=()=>send('save');redraw();
