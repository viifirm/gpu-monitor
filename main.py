import os
import psutil
import platform
import collections
from flask import Flask, render_template_string, jsonify

# --- ROBUST IMPORT ---
try:
    import pynvml
    HAS_NVIDIA_LIB = True
except ImportError:
    HAS_NVIDIA_LIB = False
    print("Notice: 'nvidia-ml-py' module not found. Running in CPU-only mode.")

# --- CONFIGURATION (Production Safe Defaults) ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 9999))
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 1000)) # 1 second
HISTORY_SIZE = 60
WARNING_THRESHOLD = 75
DANGER_THRESHOLD = 90
GPU_POWER_LIMIT = None # Auto-detect

# Colors
COLORS = {
    "safe": "#76b900",
    "warning": "#ffcc00",
    "danger": "#ff3333",
    "graph_blue": "#007bff",
    "text_bright": "#ffffff"
}

app = Flask(__name__)

# --- BACKEND MONITORING ENGINE ---

class AdvancedSystemMonitor:
    def __init__(self):
        self.history = {
            "cpu_util": collections.deque([0]*HISTORY_SIZE, maxlen=HISTORY_SIZE),
            "ram_util": collections.deque([0]*HISTORY_SIZE, maxlen=HISTORY_SIZE),
        }
        # Per-GPU history
        self.gpu_history = {}  # {gpu_index: deque}

        self.has_gpu = False
        self.gpu_handles = []
        self.gpu_names = []
        self.driver_version = "N/A"
        self.cpu_model = "Unknown CPU"

        self._init_cpu_info()
        self._init_gpu()

    def _init_cpu_info(self):
        try:
            if platform.system() == "Windows":
                import subprocess
                result = subprocess.run(
                    ['wmic', 'cpu', 'get', 'name'],
                    capture_output=True, text=True, check=True
                )
                lines = result.stdout.strip().split('\n')
                if len(lines) >= 2:
                    self.cpu_model = lines[1].strip()
            else:
                with open('/proc/cpuinfo', 'r') as f:
                    for line in f:
                        if "model name" in line:
                            raw_name = line.split(":")[1].strip()
                            self.cpu_model = raw_name.replace("(R)", "").replace("(TM)", "").replace(" CPU", "")
                            break
        except Exception:
            self.cpu_model = platform.processor()

    def _init_gpu(self):
        if HAS_NVIDIA_LIB:
            try:
                pynvml.nvmlInit()
                device_count = pynvml.nvmlDeviceGetCount()
                print(f"[GPU] Total {device_count} GPUs detected.")

                if device_count > 0:
                    for i in range(device_count):
                        handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                        name = pynvml.nvmlDeviceGetName(handle)
                        if isinstance(name, bytes):
                            name = name.decode('utf-8')
                        self.gpu_handles.append(handle)
                        self.gpu_names.append(name)
                        self.gpu_history[i] = collections.deque([0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
                        print(f"[GPU] GPU {i}: {name}")

                    self.driver_version = pynvml.nvmlSystemGetDriverVersion()
                    if isinstance(self.driver_version, bytes):
                        self.driver_version = self.driver_version.decode('utf-8')
                    self.has_gpu = True
            except Exception as e:
                print(f"NVIDIA GPU initialization failed: {e}")

    def get_top_processes(self, limit=5):
        procs = []
        try:
            for p in psutil.process_iter(['pid', 'name', 'username', 'memory_percent', 'cpu_percent']):
                try:
                    if p.info['memory_percent'] > 0.1 or p.info['cpu_percent'] > 0.1:
                        procs.append(p.info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        procs.sort(key=lambda x: x['memory_percent'], reverse=True)
        return procs[:limit]

    def get_gpu_stats(self):
        gpu_list = []

        if not self.has_gpu or not self.gpu_handles:
            return {"available": False, "count": 0, "driver": "N/A", "devices": []}

        for idx, handle in enumerate(self.gpu_handles):
            gpu_data = {
                "index": idx,
                "name": self.gpu_names[idx] if idx < len(self.gpu_names) else "N/A",
                "available": False,
                "utilization": 0,
                "history": [0]*HISTORY_SIZE,
                "vram_percent": 0,
                "vram_used_gb": 0,
                "vram_total_gb": 0,
                "temp_c": 0,
                "fan_percent": 0,
                "power_w": 0,
                "power_limit_w": 0,
                "pcie_tx_mb": 0,
                "pcie_rx_mb": 0
            }

            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

                # Update per-GPU history
                self.gpu_history[idx].append(util.gpu)

                gpu_data.update({
                    "available": True,
                    "utilization": util.gpu,
                    "history": list(self.gpu_history[idx]),
                    "vram_percent": round((mem.used / mem.total) * 100, 1),
                    "vram_used_gb": round(mem.used / (1024**3), 1),
                    "vram_total_gb": round(mem.total / (1024**3), 0),
                })

                try:
                    gpu_data["temp_c"] = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                except:
                    pass

                try:
                    gpu_data["fan_percent"] = pynvml.nvmlDeviceGetFanSpeed(handle)
                except:
                    pass

                try:
                    power_w = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                    if GPU_POWER_LIMIT is not None:
                        power_lim = GPU_POWER_LIMIT
                    else:
                        power_lim = pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000.0
                    gpu_data["power_w"] = round(power_w, 0)
                    gpu_data["power_limit_w"] = round(power_lim, 0)
                except:
                    pass

                try:
                    tx = pynvml.nvmlDeviceGetPcieThroughput(handle, pynvml.NVML_PCIE_UTIL_TX_BYTES) / (1024**2)
                    rx = pynvml.nvmlDeviceGetPcieThroughput(handle, pynvml.NVML_PCIE_UTIL_RX_BYTES) / (1024**2)
                    gpu_data["pcie_tx_mb"] = round(tx, 0)
                    gpu_data["pcie_rx_mb"] = round(rx, 0)
                except:
                    pass

            except Exception as e:
                print(f"[GPU] GPU {idx} read error: {e}")

            gpu_list.append(gpu_data)

        return {
            "available": len(gpu_list) > 0,
            "count": len(gpu_list),
            "driver": self.driver_version,
            "devices": gpu_list
        }

    def get_full_stats(self):
        # IMPORTANT: psutil.cpu_percent must be called with interval=None for non-blocking reads
        cpu_global = psutil.cpu_percent(interval=None)
        cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
        ram = psutil.virtual_memory()
        swap = psutil.swap_memory()

        try:
            if platform.system() == "Windows":
                disk = psutil.disk_usage('C:\\')
            else:
                disk = psutil.disk_usage('/')
        except Exception:
            disk = type('obj', (object,), {'percent': 0, 'used': 0, 'total': 0})()

        # Update history deques
        self.history["cpu_util"].append(cpu_global)
        self.history["ram_util"].append(ram.percent)

        stats = {
            "os": f"{platform.system()} {platform.release()}",
            "cpu": {
                "model": self.cpu_model,
                "global_usage": cpu_global,
                "history": list(self.history["cpu_util"]),
                "cores": cpu_cores,
                "count_physical": psutil.cpu_count(logical=False),
                "count_logical": psutil.cpu_count(logical=True)
            },
            "memory": {
                "ram_percent": ram.percent,
                "ram_used_gb": round(ram.used / (1024**3), 1),
                "ram_total_gb": round(ram.total / (1024**3), 0),
                "ram_history": list(self.history["ram_util"]),
                "swap_percent": swap.percent,
                "swap_used_gb": round(swap.used / (1024**3), 1),
                "swap_total_gb": round(swap.total / (1024**3), 0)
            },
            "storage": {
                 "root_percent": disk.percent,
                 "root_used_gb": round(disk.used / (1024**3), 0),
                 "root_total_gb": round(disk.total / (1024**3), 0),
            },
            "processes": self.get_top_processes(),
            "gpu": self.get_gpu_stats()
        }

        return stats

monitor = AdvancedSystemMonitor()

# --- FRONTEND ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NeuroDash // AI Monitor</title>
    <style>
        :root {
            --bg-main: #0a0a0a;
            --bg-card: #141414;
            --nvidia-green: #76b900;
            --nvidia-green-dim: #76b90044;
            --graph-blue: #007bff;
            --text-bright: #ffffff;
            --text-dim: #888888;
            --danger: #ff3333;
        }
        * { box-sizing: border-box; }
        body {
            background-color: var(--bg-main);
            color: var(--text-bright);
            font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 20px;
            overflow-x: hidden;
        }

        /* HEADER */
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding: 0 10px;}
        .header h1 { margin: 0; font-size: 1.5rem; text-transform: uppercase; letter-spacing: 2px;}
        .header .sub-info { font-size: 0.8rem; color: var(--text-dim); text-align: right;}

        /* GRID LAYOUT */
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            max-width: 1800px;
            margin: 0 auto;
            width: 100%;
        }

        .card {
            background-color: var(--bg-card);
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 8px 16px rgba(0,0,0,0.4);
            border: 1px solid #222;
            display: flex;
            flex-direction: column;
        }

        .card-header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 15px; border-bottom: 1px solid #222; padding-bottom: 10px;
        }
        .card-title { font-size: 0.9rem; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); white-space: nowrap;}
        .card-subtitle { font-size: 0.85rem; color: var(--nvidia-green); font-weight: bold; text-align: right;}

        /* GAUGE LAYOUT */
        .gauge-container {
            display: flex;
            justify-content: center;
            gap: 15px;
            align-items: center;
            flex-wrap: wrap;
        }
        .gauge-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            position: relative;
        }
        .gauge-wrapper canvas.gauge { width: 140px; height: 140px; }
        .big-value-container {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            text-align: center;
        }
        .big-value {
            font-size: 2rem;
            font-weight: 300;
            line-height: 1;
        }
        .gpu-big-value {
            font-size: 2rem;
            font-weight: 300;
            line-height: 1;
        }
        .big-unit { font-size: 1rem; color: var(--nvidia-green); }
        .sub-value { font-size: 0.75rem; color: var(--text-dim); margin-top: 3px;}

        /* GRAPHS */
        canvas.graph { width: 100%; height: 80px; }
        .graph-label {font-size: 0.7rem; color: var(--text-dim); margin-bottom: 5px; text-transform: uppercase;}

        /* METRICS GRID */
        .metrics-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px;}
        .metric-box { background: #1a1a1a; padding: 8px 5px; border-radius: 6px; text-align: center; border: 1px solid #2a2a2a;}
        .metric-box .label { font-size: 0.65rem; color: var(--text-dim); display: block; margin-bottom: 3px;}
        .metric-box .value { font-size: 1rem; font-weight: bold; color: var(--text-bright);}
        .metric-box .unit { font-size: 0.7rem; color: var(--nvidia-green);}

        /* CPU CORES */
        .cpu-cores-grid {
            display: flex;
            gap: 2px;
            margin-top: 15px;
            height: 60px;
            width: 100%;
            overflow-x: auto;
            flex-wrap: nowrap;
        }
        .core-bar-container {
            background-color: #111;
            height: 100%;
            min-width: 4px;
            flex: 1 1 0;
            position: relative;
            overflow: hidden;
        }
        .core-bar-fill {
            position: absolute;
            bottom: 0;
            left: 0;
            width: 100%;
            background-color: var(--nvidia-green);
            transition: height 0.3s ease;
        }
        .core-bar-container { background-color: #111; height: 100%; width: 100%; position: relative; overflow: hidden; border-radius: 2px;}
        .core-bar-fill { position: absolute; bottom: 0; left:0; width: 100%; background-color: var(--nvidia-green); transition: height 0.3s ease;}

        /* STORAGE */
        .storage-section { margin-top: 15px; display: flex; gap: 15px;}
        .mini-gauge-container { text-align: center; flex: 1; background: #1a1a1a; padding: 12px; border-radius: 8px;}
        .mini-gauge-container canvas.gauge { width: 100px; height: 100px; }

        /* TABLE */
        table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-top: 10px; }
        th { text-align: left; color: var(--text-dim); border-bottom: 1px solid #333; padding: 6px 0; font-size: 0.7rem;}
        td { padding: 5px 0; border-bottom: 1px solid #222; }
        .proc-mem { color: var(--nvidia-green); font-weight: bold; }
        .proc-name { color: #fff; }

        /* GPU CARDS */
        .gpu-card { grid-column: span 1; }
        .gpu-card .gauge-wrapper canvas.gauge { width: 120px; height: 120px; }

        @media (max-width: 1200px) {
            .dashboard-grid { grid-template-columns: repeat(2, 1fr); }
        }
        @media (max-width: 768px) {
            body { padding: 10px; }
            .dashboard-grid { grid-template-columns: 1fr; }
            .card-header { flex-direction: column; align-items: flex-start; gap: 5px; }
            .card-subtitle { text-align: left; }
            .gauge-container { flex-direction: column; }
            .metrics-grid { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>

    <div class="header">
        <h1><span style="color: var(--nvidia-green)">AI</span> WORKSTATION MONITOR</h1>
        <div class="sub-info" id="osInfo">Initializing...</div>
    </div>

    <div class="dashboard-grid" id="dashboard">

        <!-- CPU & MEMORY CARD -->
        <div class="card" id="cpuCard">
            <div class="card-header">
                <span class="card-title">Processor & Memory</span>
                <span class="card-subtitle" id="cpuCountInfo">Cores</span>
            </div>
            <div class="gauge-container">
                <div class="gauge-wrapper">
                    <canvas id="cpuGauge" class="gauge" width="140" height="140"></canvas>
                    <div class="big-value-container">
                        <span class="big-value" id="cpuVal">0</span><span class="big-unit">%</span>
                        <div class="sub-value">Global Load</div>
                    </div>
                </div>
                <div class="gauge-wrapper">
                    <canvas id="ramGauge" class="gauge" width="140" height="140"></canvas>
                    <div class="big-value-container">
                        <span class="big-value" id="ramVal">0</span><span class="big-unit">GB</span>
                        <div class="sub-value" id="ramTotal">of 0 GB</div>
                    </div>
                </div>
            </div>
            <div style="margin-top:12px;">
                <div class="graph-label">CPU History (60s)</div>
                <canvas id="cpuGraph" class="graph" width="400" height="80"></canvas>
            </div>
            <div style="margin-top: 12px;">
                <div class="graph-label" style="margin-bottom: 5px;">Logical Core Load</div>
                <div id="cpuCoresContainer" class="cpu-cores-grid"></div>
            </div>
        </div>

        <!-- STORAGE & PROCESSES CARD -->
        <div class="card" id="storageCard">
            <div class="card-header">
                <span class="card-title">Storage & Processes</span>
            </div>
            <div class="storage-section">
                <div class="mini-gauge-container">
                    <div class="graph-label">Main SSD</div>
                    <canvas id="ssdGauge" class="gauge" width="100" height="100"></canvas>
                    <div style="font-size:1.1rem; font-weight:bold; margin-top:5px;"><span id="ssdVal">0</span>%</div>
                    <div class="sub-value"><span id="ssdUsed">0</span> / <span id="ssdTotal">0</span> GB</div>
                </div>
                <div class="mini-gauge-container">
                    <div class="graph-label">Swap Mem</div>
                    <canvas id="swapGauge" class="gauge" width="100" height="100"></canvas>
                    <div style="font-size:1.1rem; font-weight:bold; margin-top:5px;"><span id="swapVal">0</span>%</div>
                    <div class="sub-value"><span id="swapUsed">0</span> / <span id="swapTotal">0</span> GB</div>
                </div>
            </div>
            <div style="margin-top: 20px; border-top: 1px solid #222; padding-top: 12px;">
                <span class="card-title" style="font-size: 0.8rem;">Top Resource Consumers</span>
                <table>
                    <thead>
                        <tr>
                            <th>USER</th>
                            <th>PROCESS</th>
                            <th style="text-align:right">CPU</th>
                            <th style="text-align:right">MEM</th>
                        </tr>
                    </thead>
                    <tbody id="procTable"></tbody>
                </table>
            </div>
        </div>

        <!-- GPU CARDS will be dynamically inserted here -->

    </div>

<script>
    const NVIDIA_GREEN = "{{ COLORS.safe }}";
    const GRAPH_BLUE = "{{ COLORS.graph_blue }}";
    const BG_DIM = "#222";
    const WARNING_THRESHOLD = {{ WARNING_THRESHOLD }};
    const DANGER_THRESHOLD = {{ DANGER_THRESHOLD }};
    const COLORS = {
        warning: "{{ COLORS.warning }}",
        danger: "{{ COLORS.danger }}",
        text_bright: "{{ COLORS.text_bright }}"
    };

    function getColorForValue(val) {
        if (val > DANGER_THRESHOLD) return COLORS.danger;
        if (val > WARNING_THRESHOLD) return COLORS.warning;
        return NVIDIA_GREEN;
    }

    function drawGauge(canvasId, percentage, color, thin=false) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const cx = canvas.width / 2;
        const cy = canvas.height / 2;
        const radius = thin ? canvas.width * 0.4 : canvas.width * 0.42;
        const lineWidth = thin ? 8 : 12;
        const startAngle = -Math.PI;
        const endAngle = 0;

        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.beginPath();
        ctx.arc(cx, cy, radius, startAngle, endAngle);
        ctx.lineWidth = lineWidth;
        ctx.strokeStyle = BG_DIM;
        ctx.lineCap = 'round';
        ctx.stroke();

        if (percentage > 0) {
            const currentAngle = startAngle + (percentage / 100) * (endAngle - startAngle);
            ctx.beginPath();
            ctx.arc(cx, cy, radius, startAngle, currentAngle);
            ctx.lineWidth = lineWidth;
            ctx.strokeStyle = color;
            ctx.lineCap = 'round';
            ctx.stroke();
        }
    }

    function drawGraph(canvasId, dataPoints, color) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const width = canvas.width;
        const height = canvas.height;
        const padding = 5;

        ctx.clearRect(0, 0, width, height);
        if (!dataPoints || dataPoints.length < 2) return;

        // Gradient fill
        let gradient = ctx.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, color + "44");
        gradient.addColorStop(1, color + "00");

        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.moveTo(0, height);
        const step = width / (dataPoints.length - 1);
        for (let i = 0; i < dataPoints.length; i++) {
            const y = height - (dataPoints[i] / 100 * (height - padding*2) + padding);
            ctx.lineTo(i * step, y);
        }
        ctx.lineTo(width, height);
        ctx.closePath();
        ctx.fill();

        // Line
        ctx.beginPath();
        for (let i = 0; i < dataPoints.length; i++) {
            const y = height - (dataPoints[i] / 100 * (height - padding*2) + padding);
            if (i === 0) ctx.moveTo(i * step, y);
            else ctx.lineTo(i * step, y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.stroke();
    }

    function updateCpuCores(coresData) {
        const container = document.getElementById('cpuCoresContainer');
        
        // 首次加载时创建所有核心柱
        if (container.children.length === 0) {
            coresData.forEach((_, index) => {
                const wrapper = document.createElement('div');
                wrapper.className = 'core-bar-container';
                wrapper.title = `Core ${index}: 0%`;
                wrapper.innerHTML = `<div class="core-bar-fill" id="coreBar${index}" style="height: 2%;"></div>`;
                container.appendChild(wrapper);
            });
        }
        
        // 更新每个核心的负载和颜色
        coresData.forEach((usage, index) => {
            const wrapper = container.children[index];
            if (!wrapper) return;
            
            const bar = wrapper.querySelector('.core-bar-fill');
            if (!bar) return;
            
            // 最小高度 2%，确保 0% 时也能看见
            const height = Math.max(2, usage);
            bar.style.height = height + '%';
            
            // 颜色随负载变化
            if (usage > DANGER_THRESHOLD) {
                bar.style.backgroundColor = 'var(--danger)';
            } else if (usage > WARNING_THRESHOLD) {
                bar.style.backgroundColor = COLORS.warning;
            } else {
                bar.style.backgroundColor = 'var(--nvidia-green)';
            }
            
            // 鼠标悬停显示核心编号和负载
            wrapper.title = `Core ${index}: ${usage.toFixed(1)}%`;
        });
    }

    function updateProcessTable(procs) {
        const tbody = document.getElementById('procTable');
        let html = '';
        procs.forEach(p => {
            html += `<tr><td>${p.username || '-'}</td><td class="proc-name">${(p.name || '').substring(0, 20)}</td><td style="text-align:right">${p.cpu_percent.toFixed(0)}%</td><td style="text-align:right" class="proc-mem">${p.memory_percent.toFixed(1)}%</td></tr>`;
        });
        tbody.innerHTML = html;
    }

    // --- GPU CARD MANAGEMENT ---
    let gpuCardsCreated = false;

    function createGpuCards(gpuCount) {
        const dashboard = document.getElementById('dashboard');
        for (let i = 0; i < gpuCount; i++) {
            const card = document.createElement('div');
            card.className = 'card gpu-card';
            card.id = `gpuCard${i}`;
            card.innerHTML = `
                <div class="card-header">
                    <span class="card-title">GPU ${i}</span>
                    <span class="card-subtitle" id="gpuName${i}">Initializing...</span>
                </div>
                <div class="gauge-container">
                    <div class="gauge-wrapper">
                        <canvas id="gpuUtilGauge${i}" class="gauge" width="120" height="120"></canvas>
                        <div class="big-value-container">
                            <span class="gpu-big-value" id="gpuUtilVal${i}">0</span><span class="big-unit">%</span>
                            <div class="sub-value">Compute</div>
                        </div>
                    </div>
                    <div class="gauge-wrapper">
                        <canvas id="vramGauge${i}" class="gauge" width="120" height="120"></canvas>
                        <div class="big-value-container">
                            <span class="gpu-big-value" id="vramVal${i}">0</span><span class="big-unit">GB</span>
                            <div class="sub-value" id="vramTotal${i}">of 0 GB</div>
                        </div>
                    </div>
                </div>
                <div class="metrics-grid">
                    <div class="metric-box">
                        <span class="label">TEMP</span>
                        <span class="value" id="gpuTemp${i}">0</span><span class="unit">°C</span>
                    </div>
                    <div class="metric-box">
                        <span class="label">POWER</span>
                        <span class="value" id="gpuPower${i}">0</span><span class="unit">W</span>
                    </div>
                    <div class="metric-box">
                        <span class="label">FAN</span>
                        <span class="value" id="gpuFan${i}">0</span><span class="unit">%</span>
                    </div>
                </div>
                <div style="margin-top:10px;">
                    <div class="graph-label">GPU ${i} Load History (60s)</div>
                    <canvas id="gpuGraph${i}" class="graph" width="400" height="60"></canvas>
                </div>
            `;
            dashboard.appendChild(card);
        }
        gpuCardsCreated = true;
    }

    function updateGpuCards(gpuData) {
        if (!gpuData.available || !gpuData.devices) return;

        if (!gpuCardsCreated) {
            createGpuCards(gpuData.count);
        }

        gpuData.devices.forEach((gpu, i) => {
            if (!gpu.available) return;

            // Update name in header (only first time)
            const nameEl = document.getElementById(`gpuName${i}`);
            if (nameEl && nameEl.innerText === 'Initializing...') {
                nameEl.innerText = gpu.name;
            }

            const utilColor = getColorForValue(gpu.utilization);
            const vramColor = getColorForValue(gpu.vram_percent);

            drawGauge(`gpuUtilGauge${i}`, gpu.utilization, utilColor);
            document.getElementById(`gpuUtilVal${i}`).innerText = gpu.utilization;

            drawGauge(`vramGauge${i}`, gpu.vram_percent, vramColor);
            document.getElementById(`vramVal${i}`).innerText = gpu.vram_used_gb;
            document.getElementById(`vramTotal${i}`).innerText = `of ${gpu.vram_total_gb} GB`;

            drawGraph(`gpuGraph${i}`, gpu.history, GRAPH_BLUE);

            document.getElementById(`gpuTemp${i}`).innerText = gpu.temp_c || '-';
            document.getElementById(`gpuFan${i}`).innerText = gpu.fan_percent || '-';

            const powerEl = document.getElementById(`gpuPower${i}`);
            if (gpu.power_limit_w) {
                const pct = (gpu.power_w / gpu.power_limit_w) * 100;
                powerEl.innerHTML = `<span style="color:${getColorForValue(pct)}">${gpu.power_w}/${gpu.power_limit_w}</span>`;
            } else {
                powerEl.innerText = gpu.power_w || '-';
            }
        });
    }

    // --- MAIN UPDATE LOOP ---
    let isFirstLoad = true;

    async function updateDashboard() {
        try {
            const response = await fetch('/api/full_stats');
            const data = await response.json();

            if (isFirstLoad) {
                document.getElementById('osInfo').innerText = data.os;
                document.getElementById('cpuCountInfo').innerHTML = `
                    <div style="font-size:0.75rem; color:var(--text-bright); margin-bottom:2px;">${data.cpu.model}</div>
                    ${data.cpu.count_physical} Phys / ${data.cpu.count_logical} Log
                `;
                isFirstLoad = false;
            }

            // CPU
            drawGauge('cpuGauge', data.cpu.global_usage, getColorForValue(data.cpu.global_usage));
            document.getElementById('cpuVal').innerText = data.cpu.global_usage.toFixed(1);
            drawGraph('cpuGraph', data.cpu.history, GRAPH_BLUE);
            updateCpuCores(data.cpu.cores);

            // RAM
            drawGauge('ramGauge', data.memory.ram_percent, getColorForValue(data.memory.ram_percent));
            document.getElementById('ramVal').innerText = data.memory.ram_used_gb;
            document.getElementById('ramTotal').innerText = `of ${data.memory.ram_total_gb} GB`;

            // Storage
            drawGauge('ssdGauge', data.storage.root_percent, getColorForValue(data.storage.root_percent), true);
            document.getElementById('ssdVal').innerText = data.storage.root_percent;
            document.getElementById('ssdUsed').innerText = data.storage.root_used_gb;
            document.getElementById('ssdTotal').innerText = data.storage.root_total_gb;

            // Swap
            drawGauge('swapGauge', data.memory.swap_percent, getColorForValue(data.memory.swap_percent), true);
            document.getElementById('swapVal').innerText = data.memory.swap_percent.toFixed(1);
            document.getElementById('swapUsed').innerText = data.memory.swap_used_gb;
            document.getElementById('swapTotal').innerText = data.memory.swap_total_gb;

            // Processes
            updateProcessTable(data.processes);

            // GPUs
            updateGpuCards(data.gpu);

        } catch (e) {
            console.error('Dashboard update error:', e);
        }
    }

    setInterval(updateDashboard, {{ UPDATE_INTERVAL }});
    updateDashboard();
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(
        HTML_TEMPLATE,
        UPDATE_INTERVAL=UPDATE_INTERVAL,
        WARNING_THRESHOLD=WARNING_THRESHOLD,
        DANGER_THRESHOLD=DANGER_THRESHOLD,
        COLORS=COLORS
    )

@app.route('/api/full_stats')
def full_stats():
    return jsonify(monitor.get_full_stats())

if __name__ == "__main__":
    print(f"Monitoring available at http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True)
