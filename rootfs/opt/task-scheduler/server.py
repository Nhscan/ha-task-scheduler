#!/usr/bin/env python3
"""Task Scheduler Pro v1.0.2 - Home Assistant Add-on"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import uuid

import aiohttp
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

CONFIG_PATH = Path("/config/task_scheduler")
TASKS_FILE = CONFIG_PATH / "tasks.json"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("task_scheduler")

logger.info("=" * 50)
logger.info("Task Scheduler Pro v1.0.2 Starting")
logger.info(f"Supervisor Token Present: {bool(SUPERVISOR_TOKEN)}")
logger.info("=" * 50)

CONFIG_PATH.mkdir(parents=True, exist_ok=True)


class TaskScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.tasks: Dict[str, dict] = {}
        self.history: List[dict] = []
        self.session: Optional[aiohttp.ClientSession] = None

    async def init(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.load_tasks()
        self.scheduler.start()
        logger.info("Task Scheduler initialized")

    async def close(self):
        self.scheduler.shutdown()
        if self.session:
            await self.session.close()

    def load_tasks(self):
        if TASKS_FILE.exists():
            try:
                data = json.load(open(TASKS_FILE))
                self.tasks = data.get("tasks", {})
                self.history = data.get("history", [])[-100:]
                for tid, task in self.tasks.items():
                    if task.get("enabled", True):
                        self._schedule(tid, task)
                logger.info(f"Loaded {len(self.tasks)} tasks")
            except Exception as e:
                logger.error(f"Load failed: {e}")

    def save_tasks(self):
        try:
            json.dump({"tasks": self.tasks, "history": self.history[-100:]}, 
                     open(TASKS_FILE, 'w'), indent=2, default=str)
        except Exception as e:
            logger.error(f"Save failed: {e}")

    def _schedule(self, tid: str, task: dict):
        try:
            try:
                self.scheduler.remove_job(tid)
            except:
                pass
            if not task.get("enabled", True):
                return
            
            stype = task.get("schedule_type", "interval")
            if stype == "interval":
                trigger = IntervalTrigger(**{task.get("interval_unit", "hours"): task.get("interval_value", 1)})
            elif stype == "cron":
                trigger = CronTrigger(
                    hour=task.get("cron_hour", "*"),
                    minute=task.get("cron_minute", "0"),
                    day_of_week=task.get("cron_dow", "*")
                )
            elif stype == "once":
                trigger = DateTrigger(run_date=datetime.fromisoformat(task.get("run_at", "")))
            else:
                return
            
            self.scheduler.add_job(self._run, trigger=trigger, id=tid, args=[tid], replace_existing=True)
            logger.info(f"Scheduled: {task.get('name', tid)}")
        except Exception as e:
            logger.error(f"Schedule failed: {e}")

    async def _call_api(self, method: str, endpoint: str, data: dict = None):
        url = f"http://supervisor{endpoint}"
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
        async with self.session.request(method, url, headers=headers, json=data) as r:
            if r.status >= 400:
                raise Exception(f"API error {r.status}: {await r.text()}")
            return await r.json()

    async def _run(self, tid: str):
        task = self.tasks.get(tid)
        if not task:
            return
        
        result = {"task_id": tid, "task_name": task.get("name"), "executed_at": datetime.now().isoformat(), "success": False, "message": ""}
        
        try:
            action = task.get("action_type", "")
            if action == "reboot_host":
                await self._call_api("POST", "/host/reboot")
                result.update(success=True, message="Host reboot initiated")
            elif action == "restart_ha":
                await self._call_api("POST", "/homeassistant/restart")
                result.update(success=True, message="HA restart initiated")
            elif action == "restart_addon":
                slug = task.get("addon_slug", "")
                if slug:
                    await self._call_api("POST", f"/addons/{slug}/restart")
                    result.update(success=True, message=f"Add-on {slug} restart initiated")
            elif action == "call_service":
                domain, service = task.get("service_domain", ""), task.get("service_name", "")
                if domain and service:
                    url = f"http://supervisor/core/api/services/{domain}/{service}"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json=task.get("service_data", {})) as r:
                        result.update(success=r.status < 400, message=f"Service {domain}.{service} called")
            elif action == "automation":
                aid = task.get("automation_id", "")
                if aid:
                    url = "http://supervisor/core/api/services/automation/trigger"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json={"entity_id": aid}) as r:
                        result.update(success=r.status < 400, message=f"Automation triggered")
            elif action == "script":
                sid = task.get("script_id", "")
                if sid:
                    url = "http://supervisor/core/api/services/script/turn_on"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json={"entity_id": sid}) as r:
                        result.update(success=r.status < 400, message=f"Script executed")
            elif action == "entity_control":
                entity_id = task.get("entity_id", "")
                entity_action = task.get("entity_action", "turn_on")
                if entity_id:
                    # Determine domain from entity_id
                    domain = entity_id.split(".")[0] if "." in entity_id else "homeassistant"
                    url = f"http://supervisor/core/api/services/{domain}/{entity_action}"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json={"entity_id": entity_id}) as r:
                        result.update(success=r.status < 400, message=f"Entity {entity_action} executed")
        except Exception as e:
            result["message"] = str(e)
            logger.error(f"Task failed: {e}")

        self.tasks[tid]["last_run"] = datetime.now().isoformat()
        self.tasks[tid]["last_result"] = result["success"]
        self.history.append(result)
        self.save_tasks()
        return result

    async def get_addons(self) -> List[dict]:
        """Get addons from /supervisor/info - this endpoint includes installed addons"""
        logger.info("Fetching addons from /supervisor/info...")
        try:
            result = await self._call_api("GET", "/supervisor/info")
            addons_list = result.get("data", {}).get("addons", [])
            logger.info(f"Found {len(addons_list)} addons")
            return [{"slug": a.get("slug"), "name": a.get("name"), "state": a.get("state")} for a in addons_list if isinstance(a, dict)]
        except Exception as e:
            logger.error(f"Get addons failed: {e}")
            return []

    async def get_entities(self, prefix: str) -> List[dict]:
        """Get HA entities by prefix"""
        try:
            url = "http://supervisor/core/api/states"
            headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
            async with self.session.get(url, headers=headers) as r:
                if r.status >= 400:
                    return []
                states = await r.json()
                return [s for s in states if s.get("entity_id", "").startswith(prefix)]
        except Exception as e:
            logger.error(f"Get entities failed: {e}")
            return []


scheduler = TaskScheduler()
routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    resp = web.FileResponse("/opt/task-scheduler/static/index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@routes.get("/api/tasks")
async def get_tasks(request):
    return web.json_response({"tasks": scheduler.tasks, "history": scheduler.history[-20:]})


@routes.post("/api/tasks")
async def create_task(request):
    try:
        data = await request.json()
        tid = str(uuid.uuid4())[:8]
        task = {"id": tid, "enabled": True, "created_at": datetime.now().isoformat(), **data}
        scheduler.tasks[tid] = task
        scheduler._schedule(tid, task)
        scheduler.save_tasks()
        return web.json_response({"success": True, "task": task})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=400)


@routes.put("/api/tasks/{tid}")
async def update_task(request):
    tid = request.match_info["tid"]
    if tid not in scheduler.tasks:
        return web.json_response({"error": "Not found"}, status=404)
    data = await request.json()
    scheduler.tasks[tid].update(data)
    scheduler._schedule(tid, scheduler.tasks[tid])
    scheduler.save_tasks()
    return web.json_response({"success": True})


@routes.delete("/api/tasks/{tid}")
async def delete_task(request):
    tid = request.match_info["tid"]
    if tid in scheduler.tasks:
        try:
            scheduler.scheduler.remove_job(tid)
        except:
            pass
        del scheduler.tasks[tid]
        scheduler.save_tasks()
    return web.json_response({"success": True})


@routes.post("/api/tasks/{tid}/toggle")
async def toggle_task(request):
    tid = request.match_info["tid"]
    if tid not in scheduler.tasks:
        return web.json_response({"error": "Not found"}, status=404)
    task = scheduler.tasks[tid]
    task["enabled"] = not task.get("enabled", True)
    if task["enabled"]:
        scheduler._schedule(tid, task)
    else:
        try:
            scheduler.scheduler.remove_job(tid)
        except:
            pass
    scheduler.save_tasks()
    return web.json_response({"success": True, "enabled": task["enabled"]})


@routes.post("/api/tasks/{tid}/run")
async def run_task(request):
    tid = request.match_info["tid"]
    if tid not in scheduler.tasks:
        return web.json_response({"error": "Not found"}, status=404)
    result = await scheduler._run(tid)
    return web.json_response(result)


@routes.get("/api/addons")
async def get_addons(request):
    logger.info("API: /api/addons called")
    addons = await scheduler.get_addons()
    logger.info(f"API: returning {len(addons)} addons")
    return web.json_response(addons)


@routes.get("/api/automations")
async def get_automations(request):
    return web.json_response(await scheduler.get_entities("automation."))


@routes.get("/api/scripts")
async def get_scripts(request):
    return web.json_response(await scheduler.get_entities("script."))


@routes.get("/api/entities")
async def get_controllable_entities(request):
    """Get entities that can be turned on/off (lights, switches, fans, etc.)"""
    all_entities = []
    for prefix in ["light.", "switch.", "fan.", "cover.", "climate.", "input_boolean.", "media_player."]:
        entities = await scheduler.get_entities(prefix)
        all_entities.extend(entities)
    # Sort by friendly name
    all_entities.sort(key=lambda e: e.get("attributes", {}).get("friendly_name", e.get("entity_id", "")))
    return web.json_response(all_entities)


@routes.get("/api/history")
async def get_history(request):
    return web.json_response(scheduler.history[-50:])


async def on_startup(app):
    await scheduler.init()

async def on_cleanup(app):
    await scheduler.close()


if __name__ == "__main__":
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", "/opt/task-scheduler/static")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    logger.info("Starting server on port 8099...")
    web.run_app(app, host="0.0.0.0", port=8099)
