#!/usr/bin/env python3
"""Task Scheduler Pro v1.1.0 - Home Assistant Add-on"""

import os, json, logging, uuid, asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
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

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("task_scheduler")
logger.info("=" * 50)
logger.info("Task Scheduler Pro v1.1.0 Starting")
logger.info("=" * 50)
CONFIG_PATH.mkdir(parents=True, exist_ok=True)

def hex_to_rgb(h):
    h = h.lstrip('#')
    return [int(h[i:i+2], 16) for i in (0, 2, 4)]

class TaskScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.tasks: Dict[str, dict] = {}
        self.history: List[dict] = []
        self.session: Optional[aiohttp.ClientSession] = None
        self.sun_times = {}

    async def init(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.load_tasks()
        self.scheduler.start()
        await self.update_sun_times()
        self.scheduler.add_job(self.update_sun_times, 'cron', hour=0, minute=5, id='sun_refresh')
        self.scheduler.add_job(self.check_sun_tasks, 'interval', minutes=1, id='sun_check')
        logger.info("Task Scheduler initialized")

    async def close(self):
        self.scheduler.shutdown()
        if self.session: await self.session.close()

    async def update_sun_times(self):
        try:
            url = "http://supervisor/core/api/states/sun.sun"
            headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
            async with self.session.get(url, headers=headers) as r:
                if r.status == 200:
                    data = await r.json()
                    attrs = data.get("attributes", {})
                    self.sun_times = {"sunrise": attrs.get("next_rising"), "sunset": attrs.get("next_setting")}
                    logger.info(f"Sun times: {self.sun_times}")
        except Exception as e:
            logger.error(f"Sun times error: {e}")

    async def check_sun_tasks(self):
        """Check if any sun-based tasks should run"""
        now = datetime.now()
        for tid, task in list(self.tasks.items()):
            if task.get("schedule_type") != "sun" or not task.get("enabled", True):
                continue
            event = task.get("sun_event", "sunrise")
            sun_str = self.sun_times.get(event)
            if not sun_str:
                continue
            try:
                sun_time = datetime.fromisoformat(sun_str.replace("Z", "+00:00")).replace(tzinfo=None)
                offset = task.get("sun_offset", 0)
                if task.get("sun_offset_dir") == "before":
                    offset = -offset
                target = sun_time + timedelta(minutes=offset)
                last_run = task.get("last_run")
                if last_run:
                    last = datetime.fromisoformat(last_run)
                    if (now - last).total_seconds() < 300:
                        continue
                if abs((now - target).total_seconds()) < 60:
                    logger.info(f"Sun trigger: {task.get('name')}")
                    await self._run(tid)
            except Exception as e:
                logger.error(f"Sun check error: {e}")

    def load_tasks(self):
        if TASKS_FILE.exists():
            try:
                data = json.load(open(TASKS_FILE))
                self.tasks = data.get("tasks", {})
                self.history = data.get("history", [])[-100:]
                for tid, task in self.tasks.items():
                    if task.get("enabled", True) and task.get("schedule_type") != "sun":
                        self._schedule(tid, task)
                logger.info(f"Loaded {len(self.tasks)} tasks")
            except Exception as e:
                logger.error(f"Load failed: {e}")

    def save_tasks(self):
        json.dump({"tasks": self.tasks, "history": self.history[-100:]}, open(TASKS_FILE, 'w'), indent=2, default=str)

    def _schedule(self, tid: str, task: dict):
        try:
            try: self.scheduler.remove_job(tid)
            except: pass
            if not task.get("enabled", True): return
            stype = task.get("schedule_type", "interval")
            if stype == "interval":
                trigger = IntervalTrigger(**{task.get("interval_unit", "hours"): task.get("interval_value", 1)})
            elif stype == "cron":
                trigger = CronTrigger(hour=task.get("cron_hour", 0), minute=task.get("cron_minute", 0), day_of_week=task.get("cron_dow", "*"))
            elif stype == "once":
                run_at = task.get("run_at")
                if run_at:
                    trigger = DateTrigger(run_date=datetime.fromisoformat(run_at))
                else: return
            elif stype == "sun":
                return  # Sun tasks handled by check_sun_tasks
            else: return
            self.scheduler.add_job(self._run, trigger=trigger, id=tid, args=[tid], replace_existing=True)
        except Exception as e:
            logger.error(f"Schedule error: {e}")

    async def _call(self, method: str, endpoint: str, data: dict = None):
        url = f"http://supervisor{endpoint}"
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
        async with self.session.request(method, url, headers=headers, json=data) as r:
            if r.status >= 400: raise Exception(f"API error {r.status}")
            return await r.json()

    async def _run(self, tid: str):
        task = self.tasks.get(tid)
        if not task: return
        result = {"task_id": tid, "task_name": task.get("name"), "executed_at": datetime.now().isoformat(), "success": False, "message": ""}
        try:
            action = task.get("action_type", "")
            if action == "reboot_host":
                await self._call("POST", "/host/reboot")
                result.update(success=True, message="Host reboot initiated")
            elif action == "restart_ha":
                await self._call("POST", "/homeassistant/restart")
                result.update(success=True, message="HA restart initiated")
            elif action == "restart_addon":
                slug = task.get("addon_slug")
                if slug:
                    await self._call("POST", f"/addons/{slug}/restart")
                    result.update(success=True, message=f"Add-on restart initiated")
            elif action == "entity_control":
                eid = task.get("entity_id")
                act = task.get("entity_action", "turn_on")
                if eid:
                    domain = eid.split(".")[0]
                    url = f"http://supervisor/core/api/services/{domain}/{act}"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json={"entity_id": eid}) as r:
                        result.update(success=r.status < 400, message=f"Entity {act}")
            elif action == "light_advanced":
                eid = task.get("light_entity_id")
                act = task.get("light_action", "turn_on")
                if eid:
                    svc_data = {"entity_id": eid}
                    if act == "turn_on":
                        if task.get("brightness"):
                            svc_data["brightness_pct"] = task["brightness"]
                        if task.get("color_mode") == "color" and task.get("color"):
                            svc_data["rgb_color"] = hex_to_rgb(task["color"])
                        elif task.get("color_mode") == "temp" and task.get("color_temp"):
                            svc_data["color_temp"] = task["color_temp"]
                        if task.get("transition"):
                            svc_data["transition"] = task["transition"]
                    url = f"http://supervisor/core/api/services/light/{act}"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json=svc_data) as r:
                        result.update(success=r.status < 400, message=f"Light {act}")
            elif action == "call_service":
                domain = task.get("service_domain")
                service = task.get("service_name")
                if domain and service:
                    url = f"http://supervisor/core/api/services/{domain}/{service}"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json=task.get("service_data", {})) as r:
                        result.update(success=r.status < 400, message=f"Service called")
            elif action == "automation":
                aid = task.get("automation_id")
                if aid:
                    url = "http://supervisor/core/api/services/automation/trigger"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json={"entity_id": aid}) as r:
                        result.update(success=r.status < 400, message="Automation triggered")
            elif action == "script":
                sid = task.get("script_id")
                if sid:
                    url = "http://supervisor/core/api/services/script/turn_on"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    async with self.session.post(url, headers=headers, json={"entity_id": sid}) as r:
                        result.update(success=r.status < 400, message="Script executed")
            elif action == "notify":
                svc = task.get("notify_service")
                if svc:
                    url = f"http://supervisor/core/api/services/{svc.replace('.', '/')}"
                    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
                    payload = {"title": task.get("notify_title", "Task Scheduler"), "message": task.get("notify_message", "")}
                    async with self.session.post(url, headers=headers, json=payload) as r:
                        result.update(success=r.status < 400, message="Notification sent")
        except Exception as e:
            result["message"] = str(e)
            logger.error(f"Task failed: {e}")
        self.tasks[tid]["last_run"] = datetime.now().isoformat()
        self.tasks[tid]["last_result"] = result["success"]
        self.history.append(result)
        self.save_tasks()
        return result

    async def get_addons(self) -> List[dict]:
        try:
            result = await self._call("GET", "/supervisor/info")
            return [{"slug": a.get("slug"), "name": a.get("name"), "state": a.get("state")} for a in result.get("data", {}).get("addons", [])]
        except Exception as e:
            logger.error(f"Get addons error: {e}")
            return []

    async def get_entities(self, prefix: str) -> List[dict]:
        try:
            url = "http://supervisor/core/api/states"
            headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
            async with self.session.get(url, headers=headers) as r:
                if r.status >= 400: return []
                return [s for s in await r.json() if s.get("entity_id", "").startswith(prefix)]
        except: return []

    async def get_notify_services(self) -> List[str]:
        try:
            url = "http://supervisor/core/api/services"
            headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
            async with self.session.get(url, headers=headers) as r:
                if r.status >= 400: return []
                services = await r.json()
                result = []
                for svc in services:
                    if svc.get("domain") == "notify":
                        for s in svc.get("services", {}).keys():
                            result.append(f"notify.{s}")
                return result
        except: return []

scheduler = TaskScheduler()
routes = web.RouteTableDef()

@routes.get("/")
async def index(request):
    r = web.FileResponse("/opt/task-scheduler/static/index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r

@routes.get("/api/tasks")
async def get_tasks(request):
    return web.json_response({"tasks": scheduler.tasks, "history": scheduler.history[-20:]})

@routes.post("/api/tasks")
async def create_task(request):
    data = await request.json()
    tid = str(uuid.uuid4())[:8]
    task = {"id": tid, "enabled": True, "created_at": datetime.now().isoformat(), **data}
    scheduler.tasks[tid] = task
    scheduler._schedule(tid, task)
    scheduler.save_tasks()
    return web.json_response({"success": True, "task": task})

@routes.put("/api/tasks/{tid}")
async def update_task(request):
    tid = request.match_info["tid"]
    if tid not in scheduler.tasks: return web.json_response({"error": "Not found"}, status=404)
    scheduler.tasks[tid].update(await request.json())
    scheduler._schedule(tid, scheduler.tasks[tid])
    scheduler.save_tasks()
    return web.json_response({"success": True})

@routes.delete("/api/tasks/{tid}")
async def delete_task(request):
    tid = request.match_info["tid"]
    if tid in scheduler.tasks:
        try: scheduler.scheduler.remove_job(tid)
        except: pass
        del scheduler.tasks[tid]
        scheduler.save_tasks()
    return web.json_response({"success": True})

@routes.post("/api/tasks/{tid}/toggle")
async def toggle_task(request):
    tid = request.match_info["tid"]
    if tid not in scheduler.tasks: return web.json_response({"error": "Not found"}, status=404)
    task = scheduler.tasks[tid]
    task["enabled"] = not task.get("enabled", True)
    if task["enabled"]: scheduler._schedule(tid, task)
    else:
        try: scheduler.scheduler.remove_job(tid)
        except: pass
    scheduler.save_tasks()
    return web.json_response({"success": True, "enabled": task["enabled"]})

@routes.post("/api/tasks/{tid}/run")
async def run_task(request):
    tid = request.match_info["tid"]
    if tid not in scheduler.tasks: return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(await scheduler._run(tid))

@routes.get("/api/addons")
async def get_addons(request):
    return web.json_response(await scheduler.get_addons())

@routes.get("/api/automations")
async def get_automations(request):
    return web.json_response(await scheduler.get_entities("automation."))

@routes.get("/api/scripts")
async def get_scripts(request):
    return web.json_response(await scheduler.get_entities("script."))

@routes.get("/api/entities")
async def get_entities(request):
    all_e = []
    for p in ["light.", "switch.", "fan.", "cover.", "climate.", "input_boolean.", "media_player."]:
        all_e.extend(await scheduler.get_entities(p))
    all_e.sort(key=lambda x: x.get("attributes", {}).get("friendly_name", x.get("entity_id", "")))
    return web.json_response(all_e)

@routes.get("/api/lights")
async def get_lights(request):
    lights = await scheduler.get_entities("light.")
    lights.sort(key=lambda x: x.get("attributes", {}).get("friendly_name", x.get("entity_id", "")))
    return web.json_response(lights)

@routes.get("/api/notify_services")
async def get_notify_services(request):
    return web.json_response(await scheduler.get_notify_services())

@routes.get("/api/history")
async def get_history(request):
    return web.json_response(scheduler.history[-50:])

async def on_startup(app): await scheduler.init()
async def on_cleanup(app): await scheduler.close()

if __name__ == "__main__":
    app = web.Application()
    app.add_routes(routes)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host="0.0.0.0", port=8099)
