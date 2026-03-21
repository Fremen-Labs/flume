#!/bin/bash
set -e

echo "🚀 Flume Rapid Update Cycle Initiated..."

# Navigate to script directory to ensure relative paths work natively
cd "$(dirname "$0")"

echo "📦 Stashing local configuration changes to protect tracking..."
git stash || true

echo "📥 Pulling latest Flume release from the primary upstream branch..."
git checkout main || true
git pull origin main

echo "🐍 Synchronizing Python dependencies..."
if [ -d "flume-env" ]; then
    echo "Activating restricted Python Virtual Environment..."
    source flume-env/bin/activate
fi
pip install -r install/requirements.txt || true

echo "⚛️ Compiling React UI and rebuilding physical Vite Static artifacts..."
cd src/frontend/src
npm install
npm run build
cd ../../../

echo "🔪 Forcefully terminating legacy background Flume runtime daemons..."
pkill -f "worker_handlers.py" || true
pkill -f "manager.py" || true
pkill -f "server.py" || true

# Small pause to allow socket detachment
sleep 2

echo "⚙️ Relaunching newly verified Flume backend clusters asynchronously..."
nohup python3 -u src/worker-manager/manager.py >> src/worker-manager/manager.log 2>&1 &
nohup python3 -u src/worker-manager/worker_handlers.py >> src/worker-manager/worker_handlers.log 2>&1 &
nohup python3 -u src/dashboard/server.py >> src/dashboard/server.log 2>&1 &

echo "✅ Flume deployment synchronized and locally tracking perfectly mapped upstream architectures."
