"""Thread-safe monitor state and bounded fixed-delay scheduling."""
from __future__ import annotations
import concurrent.futures, copy, subprocess, threading, time
from .probe import run_probe

def termux_notifier(status, message):
    """Best-effort Termux:API notification; never blocks the scheduler."""
    try:
        subprocess.run(["termux-notification", "--title", "Services Ping", "--content", message,
                        "--id", "services-ping", "--priority", "high" if status == "offline" else "default"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass

def _pending():
    return {"status":"pending","latency_ms":None,"checked_at":None,"message":"Ещё не проверен","http_status":None,"reachable":False,"healthy":None}

class Monitor:
    """`probe(item, timeout, connectivity=False)` is injectable for deterministic tests."""
    def __init__(self, store, probe=None, notifier=None, clock=time.monotonic, probe_fn=None):
        if probe is not None and probe_fn is not None: raise TypeError("pass probe or probe_fn, not both")
        self.store, self.probe, self.notifier, self.clock = store, probe or probe_fn or run_probe, notifier or termux_notifier, clock
        self.lock=threading.RLock(); self.config=store.config(); self.revision=1; self.paused=False
        self.internet={"status":"checking","latency_ms":None,"checked_at":None,"message":"Проверяем интернет","references":[]}
        self.results={}; self.inflight=set(); self.net_inflight=False; self.network_generation=0
        self.stop_event=threading.Event(); self.scheduler=None; self.last_notification=0; self.closed=False; self.next_net=0; self.pending_manual=False
        self.connectivity_pool=concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ping-net")
        self.service_pool=concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="ping-service")
        self.next_target={}; self.manual_targets=set(); self._seed()
    def _seed(self):
        self.results={t["id"]:_pending() for t in self.config["targets"]}
        self.internet["references"]=[dict(id=r["id"],name=r["name"],url=r["url"],status="checking",latency_ms=None,checked_at=None,message="Проверяем",http_status=None) for r in self.config["connectivity"]]
    def start(self):
        with self.lock:
            if self.scheduler: return
            self.scheduler=threading.Thread(target=self._schedule,daemon=True,name="services-ping-scheduler"); self.scheduler.start()
            self.run_connectivity(automatic=True)
    def close(self):
        with self.lock: self.closed=True
        self.stop_event.set()
        if self.scheduler: self.scheduler.join(2)
        self.connectivity_pool.shutdown(wait=False,cancel_futures=True); self.service_pool.shutdown(wait=False,cancel_futures=True)
    def _schedule(self):
        while not self.stop_event.wait(.05):
            now=self.clock()
            with self.lock:
                due_net=now>=self.next_net
            if due_net and not self.paused:
                self.run_connectivity(automatic=True)
            if not self.paused and self.internet["status"] in ("online","partial"):
                self.run_targets(due_only=True)
    def check_now(self):
        """One coalesced immediate cycle; allowed while paused."""
        if not self.run_connectivity(force_targets=True):
            with self.lock: self.pending_manual=True
        return True
    def set_paused(self, value):
        with self.lock: self.paused=bool(value)
        if not value:
            with self.lock: self.next_net=0; self.next_target={}
            self.run_connectivity(automatic=True)
    def replace_config(self, config):
        """Persist first, then atomically publish a new in-memory generation."""
        with self.lock:
            self.store.save_config(config)
            previous_targets={target["id"]:target for target in self.config["targets"]}
            previous_results=self.results
            # Keep in-flight IDs until their old-generation callbacks have exited:
            # clearing them would permit a second simultaneous same-ID probe.
            self.config=copy.deepcopy(config); self.revision+=1; self.network_generation+=1; self.next_target={}; self.manual_targets.clear(); self._seed()
            # A settings or name change does not manufacture a pending -> up event
            # for every unchanged service. Preserve its actual last measurement.
            for target in self.config["targets"]:
                old=previous_targets.get(target["id"])
                if old and all(old[key] == target[key] for key in ("kind", "address", "expected_statuses")):
                    self.results[target["id"]]=previous_results[target["id"]]
        if not self.paused: self.check_now()
    def _call(self, item, timeout, connectivity):
        try: return self.probe(item, timeout, connectivity=connectivity)
        except TypeError: return self.probe(item, timeout, connectivity)
        except Exception as exc: return {"status":"down","latency_ms":None,"http_status":None,"message":str(exc)[:300],"reachable":False,"healthy":False}
    def run_connectivity(self, force_targets=False, automatic=False):
        with self.lock:
            if self.closed or self.net_inflight: return False
            if automatic and self.paused: return False
            self.net_inflight=True; revision=self.revision; refs=copy.deepcopy(self.config["connectivity"]); timeout=self.config["settings"]["timeout_s"]
        futures=[self.connectivity_pool.submit(self._call,r,timeout,True) for r in refs]
        def finish():
            values=[]
            for f in futures:
                try: values.append(f.result())
                except Exception as exc: values.append({"status":"down","http_status":None,"latency_ms":None,"message":str(exc),"reachable":False,"healthy":False})
            self.apply_connectivity(refs,values,revision,force_targets)
        threading.Thread(target=finish,daemon=True).start(); return True
    def apply_connectivity(self, refs, values, revision=None, force_targets=False):
        """Public deterministic seam: apply one completed connectivity cycle."""
        with self.lock:
            # An old completion must release the cycle flag even though its data is stale.
            self.net_inflight=False
            rerun=self.pending_manual; self.pending_manual=False
            if self.closed or (revision is not None and revision != self.revision):
                if rerun and not self.closed: self.run_connectivity(force_targets=True)
                return False
            previous=self.internet["status"]; now=time.time(); references=[]
            any_response=any(v.get("http_status") is not None for v in values)
            all_expected=len(values)==len(refs) and all(v.get("http_status") in r.get("expected_statuses",[]) for r,v in zip(refs,values))
            status="online" if all_expected else "partial" if any_response else "offline"
            for r,v in zip(refs,values): references.append({"id":r["id"],"name":r["name"],"url":r["url"],"status":v.get("status","down"),"latency_ms":v.get("latency_ms"),"checked_at":now,"message":v.get("message",""),"http_status":v.get("http_status")})
            msg={"online":"Интернет доступен","partial":"Частичный доступ к интернету","offline":"Интернет недоступен по проверкам (это может быть DNS, firewall или captive portal)"}[status]
            self.internet={"status":status,"latency_ms":next((v.get("latency_ms") for v in values if v.get("latency_ms") is not None),None),"checked_at":now,"message":msg,"references":references}
            self.next_net=self.clock()+self.config["settings"]["internet_interval_s"]
            if status == "offline" and previous != "offline": self.network_generation += 1
            if status == "offline": self.manual_targets.clear()
            if previous != status:
                self.store.event("internet",previous,status,msg)
                notify_transition=(status == "offline" or (previous == "offline" and status in ("online", "partial")))
                if self.config["settings"]["notifications"] and notify_transition and now-self.last_notification >= 10:
                    self.last_notification=now; threading.Thread(target=self._notify,args=(status,msg),daemon=True).start()
            recovered=previous == "offline" and status in ("online","partial")
        if status in ("online","partial") and force_targets:
            with self.lock:
                self.manual_targets.update(t["id"] for t in self.config["targets"] if t["enabled"])
            self.run_targets(manual_only=True)
        elif recovered:
            with self.lock: self.next_target={}
            self.run_targets(due_only=True)
        if rerun: self.run_connectivity(force_targets=True)
        return True
    def _notify(self,status,msg):
        if self.notifier:
            try: self.notifier(status,msg)
            except Exception: pass
    def run_targets(self, due_only=False, manual_only=False):
        with self.lock:
            if self.closed or self.internet["status"] not in ("online","partial"): return 0
            if due_only and self.paused: return 0
            now=self.clock(); revision=self.revision; epoch=self.network_generation; targets=[]
            for t in self.config["targets"]:
                if len(self.inflight) >= self.config["settings"]["concurrency"]: break
                if manual_only and t["id"] not in self.manual_targets: continue
                if not t["enabled"] or t["id"] in self.inflight or (due_only and now < self.next_target.get(t["id"],0)): continue
                self.inflight.add(t["id"]); targets.append(copy.deepcopy(t))
                self.manual_targets.discard(t["id"])
        for target in targets:
            future=self.service_pool.submit(self._call,target,target["timeout_s"],False)
            future.add_done_callback(lambda f,t=target,r=revision,e=epoch: self._target_done(t,r,e,f))
        return len(targets)
    def _target_done(self,target,revision,epoch,future):
        try: result=future.result()
        except Exception as exc: result={"status":"down","latency_ms":None,"http_status":None,"message":str(exc),"reachable":False,"healthy":False}
        self.apply_target(target["id"],result,revision,epoch)
        # A manual cycle must drain every target even while automatic checks are
        # paused. Completed workers release capacity for the next bounded batch.
        self.run_targets(manual_only=True)
    def apply_target(self, target_id, result, revision=None, network_generation=None):
        """Public deterministic seam: accept a completed target probe."""
        with self.lock:
            self.inflight.discard(target_id)
            if self.closed or (revision is not None and revision != self.revision): return False
            if network_generation is not None and network_generation != self.network_generation: return False
            target=next((t for t in self.config["targets"] if t["id"]==target_id),None)
            if not target or self.internet["status"] not in ("online","partial"): return False
            self.manual_targets.discard(target_id)
            previous=self.results.get(target_id,_pending())["status"]; sample={"timestamp":time.time(),"status":result.get("status","error"),"latency_ms":result.get("latency_ms"),"http_status":result.get("http_status"),"message":result.get("message","")}
            self.results[target_id]={**sample,"checked_at":sample["timestamp"],"reachable":result.get("reachable",False),"healthy":result.get("healthy")}
            self.next_target[target_id]=self.clock()+target["interval_s"]
            self.store.sample(target_id,sample,self.config["settings"]["history_limit"])
            if previous != sample["status"]: self.store.event(target_id,previous,sample["status"],sample["message"])
            return True
    def snapshot(self):
        with self.lock:
            targets=[]; offline=self.internet["status"]=="offline"
            for t in self.config["targets"]:
                r=copy.deepcopy(self.results.get(t["id"],_pending())); stale=offline or r["status"]=="pending"
                status="disabled" if not t["enabled"] else "no_internet" if offline else r["status"]
                targets.append({**{k:t[k] for k in ("id","name","group","kind","address","enabled")},"in_flight":t["id"] in self.inflight,"stale":stale,"last_result":r if r["checked_at"] else None,"status":status,**{k:r[k] for k in ("latency_ms","checked_at","message","http_status","reachable","healthy")}})
            return {"timestamp":time.time(),"config_revision":self.revision,"paused":self.paused,"internet":copy.deepcopy(self.internet),"targets":targets,"settings":copy.deepcopy(self.config["settings"])}
