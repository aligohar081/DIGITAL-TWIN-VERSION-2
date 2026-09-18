/* ==========================================================================
   Warehouse Digital Twin — dashboard client
   The backend is authoritative: this file renders snapshots and posts commands.
   No robot position, battery level or task state is ever invented here.
   ========================================================================== */
(function () {
  "use strict";

  /* ----------------------------------------------------------------- state */
  var state = {
    layout: null,
    config: null,
    environment: {},
    robots: [],
    boxes: [],
    agents: [],
    operators: [],
    tasks: [],
    statistics: {},
    robotStatistics: [],
    options: null,
    ci: null
  };

  var selectedTaskId = null;
  var optionsSignature = "";
  var logBuffer = [];
  var LOG_CAP = 600;
  var render = { robots: {}, dash: 0 };
  var NOTIF_LEVELS = { WARNING: 1, ERROR: 1, CRITICAL: 1 };
  var NOTIF_CAP = 60;
  var notifications = [];
  var unreadNotifications = 0;
  var trendHistory = [];
  var chatHistory = [];       // [{role, content}, ...] — sent back for conversational context
  var chatUnread = 0;
  var chatOpen = false;
  var chatBusy = false;

  var COLORS = {
    floor: "#0e1417",
    grid: "#182126",
    wall: "#2c3a41",
    shelf: "#243138",
    shelfLine: "#33454e",
    storage: "#1d2a30",
    charging: "#16302f",
    loading: "#2c2716",
    unloading: "#252c1a",
    packing: "#231d2e",
    parking: "#1b2429",
    restricted: "#2b1e14",
    amber: "#ffb300",
    cyan: "#31d1c4",
    ok: "#7ddf64",
    warn: "#ff9538",
    fault: "#ff5252",
    ink: "#e7eef1",
    muted: "#7b8d95",
    faint: "#4d5f67",
    box: "#c98b3a",
    boxCarried: "#ffb300",
    boxDelivered: "#5f8f4f"
  };

  var ROBOT_TINT = ["#31d1c4", "#ffb300", "#9b8cff", "#7ddf64", "#ff9538", "#ff5252"];

  var LEGEND = [
    ["Racking", COLORS.shelf],
    ["Pick face", COLORS.storage],
    ["Charging", COLORS.charging],
    ["Loading", COLORS.loading],
    ["Unloading", COLORS.unloading],
    ["Packing", COLORS.packing],
    ["Parking", COLORS.parking],
    ["Restricted", COLORS.restricted],
    ["Route", COLORS.amber]
  ];

  var TASK_FIELDS = {
    PICK_AND_DELIVER: ["box", "source", "destination", "priority"],
    MOVE_BOX: ["box", "source", "destination", "priority"],
    DELIVER_BOX: ["box", "destination", "priority"],
    PICK_BOX: ["box", "priority"],
    MOVE_ROBOT: ["destination", "priority"],
    CHARGE_ROBOT: ["priority"],
    STOP_ROBOT: [],
    RESUME_ROBOT: [],
    // The three actor-class task types (see TRUST_LAYER.md) — "robot" has
    // no data-when in the HTML so it stays visible for these too, but the
    // backend simply ignores it for AGENT_INSPECTION/HUMAN_INSPECTION.
    AGENT_INSPECTION: ["agent"],
    HUMAN_INSPECTION: ["operator"],
    MIXED_MAINTENANCE_MISSION: ["destination", "agent", "operator", "priority"],
    AGENT_REPLAN: ["agent"],
    AGENT_AUDIT: ["agent"],
    OPERATOR_APPROVAL: ["operator"],
    OPERATOR_MAINTENANCE_SIGNOFF: ["operator"],
    BATCH_DELIVER: ["box_ids", "destination", "priority"]
  };

  var STATUS_CHIP = {
    IDLE: "idle", PLANNING: "info", MOVING: "run", WAITING: "wait",
    PICKING: "run", CARRYING: "run", DELIVERING: "run", CHARGING: "info",
    STOPPED: "wait", ERROR: "bad",
    CREATED: "idle", VALIDATING: "info", ASSIGNED: "info", IN_PROGRESS: "run",
    TRANSPORTING: "run", BLOCKED: "wait", PAUSED: "wait",
    COMPLETED: "ok", FAILED: "bad", CANCELLED: "idle",
    STORED: "idle", RESERVED: "info", DELIVERED: "ok",
    THINKING: "run", AVAILABLE: "idle", ON_TASK: "run", OFF_DUTY: "wait",
    PASS: "ok", WARN: "wait", FAIL: "bad"
  };

  /* --------------------------------------------------------------- helpers */
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function chip(value) {
    var c = el("span", "chip " + (STATUS_CHIP[value] || ""), value);
    return c;
  }
  function pos(p) { return p ? "(" + p.x + "," + p.y + ")" : "—"; }
  function titleize(value) {
    return String(value || "").replace(/_/g, " ").toLowerCase()
      .replace(/^./, function (m) { return m.toUpperCase(); });
  }
  function locationLabel(key) {
    if (!key) return "—";
    if (state.options) {
      for (var i = 0; i < state.options.locations.length; i++) {
        if (state.options.locations[i].key === key) return state.options.locations[i].label;
      }
    }
    return String(key);
  }
  function boxLabel(id) {
    for (var i = 0; i < state.boxes.length; i++) if (state.boxes[i].id === id) return state.boxes[i].name;
    return id || "—";
  }
  function robotLabel(id) {
    for (var i = 0; i < state.robots.length; i++) if (state.robots[i].id === id) return state.robots[i].name;
    return id || "AUTO";
  }
  function robotTint(id) {
    var index = 0;
    for (var i = 0; i < state.robots.length; i++) if (state.robots[i].id === id) index = i;
    return ROBOT_TINT[index % ROBOT_TINT.length];
  }

  var toastTimer = null;
  function toast(message, kind) {
    var node = $("toast");
    node.textContent = message;
    node.className = "toast" + (kind ? " " + kind : "");
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, 4200);
  }

  function api(path, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: {} };
    if (options.body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return fetch(path, init).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error(body.error || ("Request failed with " + response.status));
          error.body = body;
          throw error;
        }
        return body;
      });
    });
  }

  /* ------------------------------------------------------------- selectors */
  function fillSelect(node, items, options) {
    options = options || {};
    var previous = node.value;
    node.innerHTML = "";
    if (options.placeholder) {
      node.appendChild(new Option(options.placeholder, ""));
    }
    items.forEach(function (item) {
      node.appendChild(new Option(item.label, item.value));
    });
    if (previous && node.querySelector('option[value="' + CSS.escape(previous) + '"]')) {
      node.value = previous;
    } else if (options.selected) {
      node.value = options.selected;
    }
  }

  function syncOptions() {
    var options = state.options;
    if (!options) return;
    var signature = JSON.stringify(options) + state.boxes.length;
    if (signature === optionsSignature) return;
    optionsSignature = signature;

    fillSelect($("taskType"), options.task_types.map(function (t) {
      return { value: t.id, label: t.label };
    }));
    fillSelect($("taskRobot"), [{ value: "AUTO", label: "AUTO — let the system choose" }].concat(
      options.robots.map(function (r) { return { value: r.id, label: r.label }; })
    ));
    fillSelect($("taskBox"), options.boxes.map(function (b) {
      return { value: b.id, label: b.label + " · " + b.status.toLowerCase() };
    }));
    fillSelect($("taskBoxes"), options.boxes.map(function (b) {
      return { value: b.id, label: b.label + " · " + b.status.toLowerCase() };
    }));
    fillSelect($("taskAgent"), [{ value: "AUTO", label: "AUTO — let the system choose" }].concat(
      (options.agents || []).map(function (a) { return { value: a.id, label: a.label }; })
    ));
    fillSelect($("taskOperator"), [{ value: "AUTO", label: "AUTO — let the system choose" }].concat(
      (options.operators || []).map(function (o) { return { value: o.id, label: o.label }; })
    ));
    fillSelect($("taskSecondOperator"), [{ value: "AUTO", label: "AUTO — let the system choose" }].concat(
      (options.operators || []).map(function (o) { return { value: o.id, label: o.label }; })
    ));
    fillSelect($("robotClass"), (options.robot_classes || []).map(function (c) {
      return { value: c.id, label: c.label };
    }), { selected: "AMR" });
    fillSelect($("operatorRole"), (options.operator_roles || []).map(function (r) {
      return { value: r.id, label: r.label };
    }), { placeholder: "None — manual certifications/shift" });
    var certList = $("knownCertifications");
    certList.innerHTML = "";
    (options.known_certifications || []).forEach(function (cert) {
      certList.appendChild(new Option(cert, cert));
    });
    fillSelect($("scheduleType"), options.task_types.map(function (t) {
      return { value: t.id, label: t.label };
    }));
    fillSelect($("decisionType"), options.task_types.map(function (t) {
      return { value: t.id, label: t.label };
    }), { placeholder: "Any" });
    var locations = options.locations.map(function (l) { return { value: l.key, label: l.label }; });
    fillSelect($("taskSource"), locations, { placeholder: "Any" });
    fillSelect($("taskDestination"), locations);
    fillSelect($("boxDestination"), locations, { placeholder: "Unassigned" });
    fillSelect($("taskPriority"), options.priorities.map(function (p) {
      return { value: p, label: titleize(p) };
    }), { selected: "NORMAL" });

    fillSelect($("speedSelect"), options.speeds.map(function (s) {
      return { value: String(s), label: s + "×" };
    }), { selected: String(state.environment.simulation_speed || 1) });

    fillSelect($("logRobot"), options.robots.map(function (r) {
      return { value: r.id, label: r.label };
    }), { placeholder: "All robots" });

    var levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
    if ($("logLevel").options.length <= 1) {
      fillSelect($("logLevel"), levels.map(function (l) {
        return { value: l, label: titleize(l) + " and up" };
      }), { placeholder: "All levels" });
    }
    var categories = ["SYSTEM", "ENVIRONMENT", "ROBOT", "BOX", "TASK", "PLANNER",
      "NAVIGATION", "COLLISION", "BATTERY", "WAREHOUSE", "USER", "CI", "DIGITAL_TWIN"];
    if ($("logCategory").options.length <= 1) {
      fillSelect($("logCategory"), categories.map(function (c) {
        return { value: c, label: titleize(c) };
      }), { placeholder: "All categories" });
    }
    applyTaskFieldVisibility();
  }

  function syncTaskFilter() {
    var node = $("logTask");
    var ids = state.tasks.map(function (t) { return { value: t.id, label: t.id + " · " + t.type.toLowerCase() }; });
    var signature = ids.map(function (i) { return i.value; }).join(",");
    if (node.dataset.signature === signature) return;
    node.dataset.signature = signature;
    fillSelect(node, ids, { placeholder: "All tasks" });
  }

  function applyTaskFieldVisibility() {
    var type = $("taskType").value;
    var allowed = TASK_FIELDS[type] || [];
    var fields = document.querySelectorAll("#taskForm [data-when]");
    for (var i = 0; i < fields.length; i++) {
      fields[i].hidden = allowed.indexOf(fields[i].dataset.when) === -1;
    }
  }

  /* -------------------------------------------------------------- rendering */
  function renderHeader() {
    var env = state.environment;
    var stats = state.statistics;
    var lamp = $("statusLamp");
    var status = env.simulation_status || "STOPPED";
    lamp.className = "lamp " + (
      status === "RUNNING" ? "running" :
      status === "PAUSED" ? "paused" :
      status === "EMERGENCY_STOP" ? "emergency" : "stopped"
    );
    $("simStatus").textContent = status.replace("_", " ").toLowerCase();
    $("statRobots").textContent = stats.robots || 0;
    $("statBoxes").textContent = stats.boxes || 0;
    $("statActive").textContent = stats.active_tasks || 0;
    $("statCompleted").textContent = stats.completed_tasks || 0;
    $("statCollisions").textContent = stats.collisions || 0;
    $("statBattery").textContent = (stats.average_battery !== undefined ? stats.average_battery : "—") + "%";
    $("statClock").textContent = (env.simulation_time || 0).toFixed(1) + "s · t" + (env.tick || 0);

    var emergency = status === "EMERGENCY_STOP";
    $("estopBtn").classList.toggle("armed", emergency);
    $("resumeBtn").hidden = !emergency;
  }

  function renderStats() {
    var s = state.statistics;
    var items = [
      ["Robots", s.robots, ""],
      ["Active", s.active_robots, "accent"],
      ["Idle", s.idle_robots, ""],
      ["Charging", s.charging_robots, ""],
      ["Stopped", s.stopped_robots, s.stopped_robots ? "bad" : ""],
      ["Avg battery", (s.average_battery || 0) + "%", ""],
      ["Boxes", s.boxes, ""],
      ["Stored", s.boxes_stored, ""],
      ["In transit", s.boxes_in_transit, "accent"],
      ["Delivered", s.boxes_delivered, "good"],
      ["Active tasks", s.active_tasks, "accent"],
      ["Pending", s.pending_tasks, ""],
      ["Completed", s.completed_tasks, "good"],
      ["Failed", s.failed_tasks, s.failed_tasks ? "bad" : ""],
      ["Collisions", s.collisions, s.collisions ? "bad" : "good"],
      ["Conflicts avoided", s.collisions_avoided, ""],
      ["Distance", s.total_distance, ""],
      ["Charge cycles", s.charging_sessions, ""],
      ["Paths built", s.navigation ? s.navigation.paths_computed : 0, ""],
      ["Replans", s.navigation ? s.navigation.replans : 0, ""],
      ["CI runs", s.ci_runs, ""]
    ];
    var grid = $("statGrid");
    grid.innerHTML = "";
    items.forEach(function (item) {
      var cell = el("div", "stat");
      cell.appendChild(el("span", "stat-label", item[0]));
      cell.appendChild(el("div", "stat-value " + item[2], item[1] === undefined ? "—" : String(item[1])));
      grid.appendChild(cell);
    });
  }

  function renderTasks() {
    var board = $("taskBoard");
    board.innerHTML = "";
    $("taskCount").textContent = state.tasks.length + " tracked";
    if (!state.tasks.length) {
      board.appendChild(el("p", "form-note", "No tasks yet. Create one on the left to put a robot to work."));
      return;
    }
    state.tasks.forEach(function (task) {
      var card = el("div", "task-card" + (task.id === selectedTaskId ? " selected" : ""));
      card.dataset.priority = task.priority;
      card.dataset.task = task.id;

      var top = el("div", "task-top");
      top.appendChild(el("span", "task-id", task.id));
      top.appendChild(chip(task.status));
      card.appendChild(top);

      var line = el("p", "task-line");
      line.innerHTML = "<b>" + robotLabel(task.robot_id || task.requested_robot) + "</b> · " +
        titleize(task.type);
      card.appendChild(line);

      var detail = el("p", "task-line", describeTask(task));
      card.appendChild(detail);

      var bar = el("div", "bar");
      var fill = el("i");
      fill.style.width = task.progress + "%";
      bar.appendChild(fill);
      card.appendChild(bar);

      if (task.current_action) {
        card.appendChild(el("p", "task-line", task.current_action));
      } else if (task.error) {
        var err = el("p", "task-line", task.error);
        err.style.color = COLORS.fault;
        card.appendChild(err);
      }

      card.addEventListener("click", function () {
        selectedTaskId = task.id;
        renderTasks();
        renderTimeline();
        openTaskModal(task.id);
      });
      board.appendChild(card);
    });
  }

  function describeTask(task) {
    switch (task.type) {
      case "PICK_AND_DELIVER":
      case "MOVE_BOX":
        return boxLabel(task.box_id) + " → " + locationLabel(task.destination);
      case "DELIVER_BOX":
        return "deliver " + boxLabel(task.box_id) + " → " + locationLabel(task.destination);
      case "PICK_BOX":
        return "pick " + boxLabel(task.box_id);
      case "MOVE_ROBOT":
        return "drive to " + locationLabel(task.destination);
      case "CHARGE_ROBOT":
        return "charge to 100%";
      case "AGENT_REPLAN":
        return "review queue & recommend assignment";
      case "AGENT_AUDIT":
        return "audit recent logs & CI";
      case "OPERATOR_APPROVAL":
        return "approve" + (task.robot_id ? " " + robotLabel(task.robot_id) : "");
      case "OPERATOR_MAINTENANCE_SIGNOFF":
        return "maintenance sign-off" + (task.robot_id ? " " + robotLabel(task.robot_id) : "");
      default:
        return titleize(task.type);
    }
  }

  /* Per-robot task-type restriction — Robot.allowed_task_types (backend/
     robot.py), enforced by backend/eligibility.py. Unchecked = unrestricted
     (the default). Every toggle posts the FULL new list immediately —
     there's no separate "save" step. */
  function capabilityEditor(robot) {
    var options = (state.options && state.options.robot_capability_task_types) || [];
    var wrap = el("div", "cap-editor");
    wrap.appendChild(el("span", "cap-label", "Capabilities"));
    var list = el("div", "cap-list");
    var restricted = !!(robot.allowed_task_types && robot.allowed_task_types.length);
    options.forEach(function (opt) {
      var id = "cap-" + robot.id + "-" + opt.id;
      var label = el("label", "cap-item");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.id = id;
      box.checked = !restricted || robot.allowed_task_types.indexOf(opt.id) !== -1;
      box.addEventListener("change", function () {
        var checked = Array.prototype.slice.call(list.querySelectorAll("input[type=checkbox]"))
          .filter(function (b) { return b.checked; }).length;
        var next;
        if (checked === options.length || checked === 0) {
          // All on (or all off, which would strand the robot) both mean
          // "unrestricted" — send an empty list rather than trap the user.
          next = [];
        } else {
          next = options
            .filter(function (o) { return list.querySelector("#cap-" + robot.id + "-" + o.id).checked; })
            .map(function (o) { return o.id; });
        }
        api("/api/robots/" + robot.id + "/capabilities", { method: "POST", body: { allowed_task_types: next } })
          .then(function () {
            toast(robot.name + ": capabilities updated" + (next.length ? " (" + next.length + " allowed)" : " (unrestricted)"), "good");
          })
          .catch(function (error) { toast(error.message, "bad"); });
      });
      label.appendChild(box);
      label.appendChild(document.createTextNode(opt.label));
      list.appendChild(label);
    });
    wrap.appendChild(list);
    return wrap;
  }

  function renderRobots() {
    var grid = $("robotGrid");
    grid.innerHTML = "";
    var statsById = {};
    state.robotStatistics.forEach(function (s) { statsById[s.id] = s; });

    state.robots.forEach(function (robot) {
      var card = el("div", "robot-card");
      var head = el("div", "robot-head");
      var name = el("div", "robot-name");
      var dot = el("i");
      dot.style.background = robotTint(robot.id);
      name.appendChild(dot);
      name.appendChild(document.createTextNode(robot.name));
      head.appendChild(name);
      head.appendChild(chip(robot.status));
      card.appendChild(head);

      var level = robot.battery <= 8 ? "critical" : robot.battery <= 20 ? "low" : "";
      var battery = el("div", "battery " + level);
      var bar = el("div", "bar");
      var fill = el("i");
      fill.style.width = robot.battery + "%";
      bar.appendChild(fill);
      battery.appendChild(bar);
      battery.appendChild(el("span", null, robot.battery + "%"));
      card.appendChild(battery);

      var stats = statsById[robot.id] || {};
      var kv = el("dl", "kv");
      [
        ["Position", pos(robot.position)],
        ["Heading", titleize(robot.orientation)],
        ["Task", robot.current_task || "—"],
        ["Carrying", robot.carrying_box ? boxLabel(robot.carrying_box) : "none"],
        ["Target", robot.target_name || pos(robot.target_position)],
        ["Route left", robot.current_path.length + " cells"],
        ["Distance", (stats.total_distance || 0) + " cells"],
        ["Completed", (stats.tasks_completed || 0) + " tasks"],
        ["Failed", (stats.tasks_failed || 0) + " tasks"],
        ["Delivered", (stats.boxes_delivered || 0) + " boxes"],
        ["Waits", (stats.wait_events || 0) + " / replans " + (stats.replans || 0)],
        ["Charges", (stats.charging_sessions || 0)]
      ].forEach(function (pair) {
        kv.appendChild(el("dt", null, pair[0]));
        kv.appendChild(el("dd", null, String(pair[1])));
      });
      card.appendChild(kv);

      if (robot.blocked_by) {
        card.appendChild(el("p", "form-note", "Waiting for " + robotLabel(robot.blocked_by)));
      }
      if (robot.last_error) {
        var err = el("p", "form-note bad", robot.last_error);
        card.appendChild(err);
      }

      card.appendChild(capabilityEditor(robot));

      var row = el("div", "btn-row");
      [["Stop", "stop"], ["Resume", "resume"], ["Charge", "charge"], ["Reset", "reset"]]
        .forEach(function (pair) {
          var button = el("button", "btn small", pair[0]);
          button.addEventListener("click", function () {
            api("/api/robots/" + robot.id + "/" + pair[1], { method: "POST" })
              .then(function () { toast(robot.name + ": " + pair[0].toLowerCase() + " sent", "good"); })
              .catch(function (error) { toast(error.message, "bad"); });
          });
          row.appendChild(button);
        });
      card.appendChild(row);
      grid.appendChild(card);
    });
  }

  function renderBoxes() {
    var body = $("boxTable").querySelector("tbody");
    body.innerHTML = "";
    state.boxes.forEach(function (box) {
      var row = el("tr");
      row.appendChild(el("td", null, box.name));
      var statusCell = el("td");
      statusCell.appendChild(chip(box.status));
      row.appendChild(statusCell);
      row.appendChild(el("td", "mono", pos(box.position)));
      row.appendChild(el("td", "mono", box.weight + " kg"));
      row.appendChild(el("td", null, locationLabel(box.destination)));
      row.appendChild(el("td", null, box.assigned_robot ? robotLabel(box.assigned_robot) : "—"));
      body.appendChild(row);
    });
  }

  function renderAgents() {
    var table = $("agentTable");
    if (!table) return;
    var body = table.querySelector("tbody");
    body.innerHTML = "";
    state.agents.forEach(function (agent) {
      var row = el("tr");
      row.appendChild(el("td", null, agent.name));
      var statusCell = el("td");
      statusCell.appendChild(chip(agent.status));
      row.appendChild(statusCell);
      row.appendChild(el("td", "mono", agent.model_version));
      row.appendChild(el("td", "mono", agent.completed_tasks + " done / " + agent.failed_tasks + " failed"));
      body.appendChild(row);
    });
  }

  function renderOperators() {
    var table = $("operatorTable");
    if (!table) return;
    var body = table.querySelector("tbody");
    body.innerHTML = "";
    state.operators.forEach(function (operator) {
      var row = el("tr");
      row.appendChild(el("td", null, operator.name));
      row.appendChild(el("td", null, operator.role ? titleize(operator.role) : "—"));
      var statusCell = el("td");
      statusCell.appendChild(chip(operator.status));
      row.appendChild(statusCell);
      row.appendChild(el("td", null, operator.certifications.join(", ") || "—"));
      row.appendChild(el("td", "mono", operator.shift_start_hour != null
        ? operator.shift_start_hour + "-" + operator.shift_end_hour : "always"));
      row.appendChild(el("td", "mono", operator.completed_tasks + " done / " + operator.failed_tasks + " failed"));
      body.appendChild(row);
    });
  }

  function activeTaskId() {
    if (selectedTaskId) return selectedTaskId;
    for (var i = 0; i < state.tasks.length; i++) {
      if (["ASSIGNED", "IN_PROGRESS", "PICKING", "TRANSPORTING", "DELIVERING", "BLOCKED"]
        .indexOf(state.tasks[i].status) !== -1) return state.tasks[i].id;
    }
    return state.tasks.length ? state.tasks[0].id : null;
  }

  function findTask(id) {
    for (var i = 0; i < state.tasks.length; i++) if (state.tasks[i].id === id) return state.tasks[i];
    return null;
  }

  function renderTimeline() {
    var list = $("timeline");
    var id = activeTaskId();
    var task = id ? findTask(id) : null;
    $("timelineTask").textContent = task ? task.id : "";
    list.innerHTML = "";
    if (!task || !task.history.length) {
      list.appendChild(el("li", "empty", "Select a task to trace its execution."));
      return;
    }
    task.history.forEach(function (entry) {
      var item = el("li");
      item.appendChild(el("span", "t", entry.time));
      item.appendChild(el("span", "s", entry.status.replace("_", " ")));
      item.appendChild(el("span", "m", entry.message));
      list.appendChild(item);
    });
  }

  function renderCi() {
    var summary = state.ci;
    var status = $("ciStatus");
    var list = $("ciList");
    if (!summary) {
      status.textContent = "not run";
      status.className = "ci-status";
      return;
    }
    status.textContent = summary.pipeline + " " + summary.status;
    status.className = "ci-status " + (summary.status === "PASSED" ? "pass" : "fail");
    list.innerHTML = "";
    summary.checks.forEach(function (check) {
      var item = el("li", check.passed ? "pass" : "fail");
      item.appendChild(el("span", "mark", check.passed ? "✓" : "✗"));
      item.appendChild(el("span", null, check.name));
      item.appendChild(el("span", "dur", check.duration_ms + " ms"));
      if (!check.passed) item.appendChild(el("span", "why", check.message));
      list.appendChild(item);
    });
  }

  /* ----------------------------------------------------------- fleet load */
  function renderFleetLoad(rows) {
    var wrap = $("fleetLoad");
    wrap.innerHTML = "";
    if (!rows || !rows.length) {
      wrap.appendChild(el("p", "form-note", "No robots yet."));
      return;
    }
    var max = Math.max(1, Math.max.apply(null, rows.map(function (r) { return r.workload; })));
    rows.forEach(function (row) {
      var item = el("div", "load-row");
      var label = el("div", "load-label");
      label.appendChild(document.createTextNode(row.robot));
      label.appendChild(el("span", "load-count", row.workload + " job" + (row.workload === 1 ? "" : "s")));
      item.appendChild(label);
      var bar = el("div", "bar load-bar");
      var fill = el("i");
      fill.style.width = (100 * row.workload / max) + "%";
      if (row.workload === 0) fill.style.width = "2px";
      bar.appendChild(fill);
      item.appendChild(bar);
      wrap.appendChild(item);
    });
  }

  function refreshFleetLoad() {
    api("/api/fleet/load").then(function (body) {
      renderFleetLoad(body.fleet_load);
    }).catch(function () { /* non-critical panel — stay quiet on failure */ });
  }

  /* ------------------------------------------------------------ schedules */
  function renderSchedules(schedules) {
    var list = $("scheduleList");
    list.innerHTML = "";
    if (!schedules || !schedules.length) {
      list.appendChild(el("li", "empty", "No recurring tasks yet."));
      return;
    }
    schedules.forEach(function (schedule) {
      var item = el("li", "schedule-item" + (schedule.enabled ? "" : " disabled"));
      var head = el("div", "schedule-head");
      head.appendChild(el("span", "schedule-type", titleize(schedule.payload.type)));
      head.appendChild(el("span", "schedule-interval", "every " + schedule.interval_seconds + "s"));
      item.appendChild(head);
      item.appendChild(el("p", "form-note", "Ran " + schedule.run_count + "× · " +
        (schedule.enabled ? "enabled" : "paused")));
      var row = el("div", "btn-row");
      var toggleBtn = el("button", "btn small", schedule.enabled ? "Pause" : "Resume");
      toggleBtn.addEventListener("click", function () {
        api("/api/schedules/" + schedule.id + "/toggle", { method: "POST" })
          .then(refreshSchedules)
          .catch(function (error) { toast(error.message, "bad"); });
      });
      var removeBtn = el("button", "btn small", "Remove");
      removeBtn.addEventListener("click", function () {
        api("/api/schedules/" + schedule.id, { method: "DELETE" })
          .then(refreshSchedules)
          .catch(function (error) { toast(error.message, "bad"); });
      });
      row.appendChild(toggleBtn);
      row.appendChild(removeBtn);
      item.appendChild(row);
      list.appendChild(item);
    });
  }

  function refreshSchedules() {
    api("/api/schedules").then(function (body) {
      renderSchedules(body.schedules);
    }).catch(function () { /* non-critical panel — stay quiet on failure */ });
  }

  /* ---------------------------------------------------------------- trends */
  function sparkline(values, color) {
    var canvas = document.createElement("canvas");
    canvas.width = 240;
    canvas.height = 48;
    var ctx = canvas.getContext("2d");
    if (!values.length) return canvas;
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var span = max - min || 1;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach(function (v, i) {
      var x = (i / Math.max(1, values.length - 1)) * canvas.width;
      var y = canvas.height - ((v - min) / span) * (canvas.height - 6) - 3;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    return canvas;
  }

  function renderTrends(history) {
    var grid = $("trendGrid");
    grid.innerHTML = "";
    if (!history || history.length < 2) {
      grid.appendChild(el("p", "form-note", "Trends appear once the simulation has run for a few ticks."));
      return;
    }
    [
      ["Avg battery", history.map(function (h) { return h.average_battery; }), COLORS.ok, "%"],
      ["Active tasks", history.map(function (h) { return h.active_tasks; }), COLORS.cyan, ""],
      ["Collisions", history.map(function (h) { return h.collisions; }), COLORS.fault, ""]
    ].forEach(function (spec) {
      var box = el("div", "trend-box");
      var head = el("div", "trend-head");
      head.appendChild(el("span", null, spec[0]));
      var last = spec[1][spec[1].length - 1];
      head.appendChild(el("span", "trend-value", last + spec[3]));
      box.appendChild(head);
      box.appendChild(sparkline(spec[1], spec[2]));
      grid.appendChild(box);
    });
  }

  function refreshTrends() {
    api("/api/statistics/history").then(function (body) {
      trendHistory = body.history || [];
      renderTrends(trendHistory);
    }).catch(function () { /* non-critical panel — stay quiet on failure */ });
  }

  /* ------------------------------------------------------- decision search */
  function renderDecisions(records) {
    var body = $("decisionTable").querySelector("tbody");
    body.innerHTML = "";
    if (!records || !records.length) {
      var row = el("tr");
      row.appendChild(el("td", "empty", "No matching tasks."));
      row.querySelector("td").colSpan = 7;
      body.appendChild(row);
      return;
    }
    records.forEach(function (record) {
      var task = record.task || {};
      var entities = record.entities || {};
      var verdict = record.verdict || "N/A";
      var row = el("tr", "verdict-" + verdict.toLowerCase());
      row.appendChild(el("td", "mono", task.task_id || "?"));
      row.appendChild(el("td", null, titleize(task.type || "?")));
      var verdictCell = el("td");
      verdictCell.appendChild(chip(verdict));
      row.appendChild(verdictCell);
      row.appendChild(el("td", null, (entities.robot && entities.robot.name) || task.requested_robot || "—"));
      row.appendChild(el("td", null, (entities.agent && entities.agent.name) || task.requested_agent || "—"));
      row.appendChild(el("td", null, (entities.operator && entities.operator.name) || task.requested_operator || "—"));
      row.appendChild(el("td", null, task.summary || task.message || ""));
      body.appendChild(row);
    });
  }

  function runDecisionSearch() {
    var params = [];
    [
      ["robot", $("decisionRobot").value],
      ["agent", $("decisionAgent").value],
      ["operator", $("decisionOperator").value],
      ["type", $("decisionType").value],
      ["verdict", $("decisionVerdict").value],
      ["battery_below", $("decisionBatteryBelow").value]
    ].forEach(function (pair) {
      if (pair[1]) params.push(pair[0] + "=" + encodeURIComponent(pair[1]));
    });
    api("/api/decisions" + (params.length ? "?" + params.join("&") : ""))
      .then(function (body) { renderDecisions(body.decisions); })
      .catch(function (error) { toast(error.message, "bad"); });
  }

  /* -------------------------------------------------------------- policies */
  function renderPolicies(policy) {
    $("policySource").textContent = policy.source_file ? "policies.yaml" : "built-in defaults";
    $("policyLlmToggle").checked = !!(policy.config && policy.config.AGENT_LLM_ENABLED);
    var riskPercent = Math.round((policy.config && policy.config.COLLISION_RISK || 0) * 100);
    $("collisionRiskSlider").value = String(riskPercent);
    $("collisionRiskValue").textContent = riskPercent + "%";
    var falseSuccessPercent = Math.round((policy.config && policy.config.FALSE_SUCCESS_RISK || 0) * 100);
    $("falseSuccessRiskSlider").value = String(falseSuccessPercent);
    $("falseSuccessRiskValue").textContent = falseSuccessPercent + "%";
    var kv = $("policyKv");
    kv.innerHTML = "";
    var cfg = policy.config || {};
    [
      ["Battery low / critical", cfg.BATTERY_LOW + "% / " + cfg.BATTERY_CRITICAL + "%"],
      ["Maintenance thresholds", cfg.MAINTENANCE_DISTANCE_THRESHOLD + " cells / " + cfg.MAINTENANCE_CHARGE_CYCLES_THRESHOLD + " charges"],
      ["Approved firmware", (policy.approved_firmware_versions || []).join(", ")],
      ["Approved agent models", (policy.approved_agent_models || []).join(", ")],
      ["Robot classes", Object.keys(policy.robot_classes || {}).join(", ")]
    ].forEach(function (pair) {
      kv.appendChild(el("dt", null, pair[0]));
      kv.appendChild(el("dd", null, String(pair[1])));
    });
  }

  function refreshPolicies() {
    api("/api/policies").then(renderPolicies).catch(function () { /* non-critical panel */ });
  }

  function openTaskModal(id) {
    var task = findTask(id);
    if (!task) return;
    $("modalTitle").textContent = task.id + " · " + titleize(task.type);
    var body = $("modalBody");
    body.innerHTML = "";

    var sub = el("p", "modal-sub",
      robotLabel(task.robot_id || task.requested_robot) + " · " + task.priority.toLowerCase() +
      " priority · " + task.progress + "% complete" +
      (task.battery_estimate ? " · needs ~" + task.battery_estimate + "% battery" : "") +
      (task.replans ? " · " + task.replans + " replans" : ""));
    body.appendChild(sub);

    var meta = el("dl", "kv");
    [
      ["Status", task.status],
      ["Box", task.box_id ? boxLabel(task.box_id) : "—"],
      ["Source", locationLabel(task.source)],
      ["Destination", locationLabel(task.destination)],
      ["Created", task.created_at],
      ["Started", task.started_at || "—"],
      ["Finished", task.completed_at || "—"],
      ["Recharged first", task.recharged_before_start ? "yes" : "no"]
    ].forEach(function (pair) {
      meta.appendChild(el("dt", null, pair[0]));
      meta.appendChild(el("dd", null, String(pair[1])));
    });
    body.appendChild(meta);

    body.appendChild(el("h4", null, "Plan"));
    var plan = el("ol", "plan");
    if (!task.actions.length) {
      plan.appendChild(el("li", null, "Waiting for a robot before planning."));
    }
    task.actions.forEach(function (action, index) {
      var cls = action.done ? "done" : (index === task.action_index ? "current" : "");
      var item = el("li", cls);
      item.appendChild(el("span", "n", action.done ? "✓" : String(index + 1)));
      item.appendChild(el("span", null, action.description));
      item.appendChild(el("span", "dur", action.target ? pos(action.target) : ""));
      plan.appendChild(item);
    });
    body.appendChild(plan);

    body.appendChild(el("h4", null, "Execution history"));
    var history = el("ol", "timeline");
    task.history.forEach(function (entry) {
      var item = el("li");
      item.appendChild(el("span", "t", entry.time));
      item.appendChild(el("span", "s", entry.status.replace("_", " ")));
      item.appendChild(el("span", "m", entry.message));
      history.appendChild(item);
    });
    body.appendChild(history);

    if (task.error) {
      var error = el("p", "form-note bad", task.error);
      body.appendChild(error);
    }

    var actions = el("div", "modal-actions");
    if (["COMPLETED", "FAILED", "CANCELLED"].indexOf(task.status) === -1) {
      [["Pause", "pause"], ["Resume", "resume"], ["Cancel", "cancel"]].forEach(function (pair) {
        var button = el("button", "btn small", pair[0]);
        button.addEventListener("click", function () {
          api("/api/tasks/" + task.id + "/" + pair[1], { method: "POST" })
            .then(function () { toast(task.id + " " + pair[1] + "ed", "good"); closeModal(); })
            .catch(function (error) { toast(error.message, "bad"); });
        });
        actions.appendChild(button);
      });
    }
    body.appendChild(actions);
    $("taskModal").hidden = false;
  }

  function closeModal() { $("taskModal").hidden = true; }

  /* ------------------------------------------------------------------ logs */
  function logMatches(record) {
    var levelOrder = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
    var minLevel = $("logLevel").value;
    if (minLevel && levelOrder[record.level] < levelOrder[minLevel]) return false;
    var category = $("logCategory").value;
    if (category && record.category !== category) return false;
    var robot = $("logRobot").value;
    if (robot && record.robot_id !== robot) return false;
    var task = $("logTask").value;
    if (task && record.task_id !== task) return false;
    var search = $("logSearch").value.trim().toLowerCase();
    if (search && record.message.toLowerCase().indexOf(search) === -1) return false;
    return true;
  }

  function logRow(record) {
    var row = el("div", "log-row " + record.level);
    row.appendChild(el("span", "lt", record.time || ""));
    row.appendChild(el("span", "ll", record.level.slice(0, 4)));
    row.appendChild(el("span", "lc", record.category));
    row.appendChild(el("span", "lm", record.message));
    return row;
  }

  function appendLog(record) {
    logBuffer.push(record);
    if (logBuffer.length > LOG_CAP) logBuffer = logBuffer.slice(-LOG_CAP);
    if (NOTIF_LEVELS[record.level]) pushNotification(record);
    if (!logMatches(record)) return;
    var stream = $("logStream");
    var pinned = $("logAutoscroll").checked;
    stream.appendChild(logRow(record));
    while (stream.childNodes.length > LOG_CAP) stream.removeChild(stream.firstChild);
    if (pinned) stream.scrollTop = stream.scrollHeight;
  }

  /* ------------------------------------------------------------ notifications
     Every WARNING-and-above log record (battery low, maintenance due, a
     robot's authorization changing mid-task, an operator's shift toggling,
     ...) also lands here — a persistent bell/panel on top of the ephemeral
     toast, so something noteworthy that happens while you're looking
     elsewhere is still visible afterwards. */
  function pushNotification(record) {
    notifications.unshift(record);
    if (notifications.length > NOTIF_CAP) notifications.length = NOTIF_CAP;
    unreadNotifications++;
    renderNotifications();
    renderNotifBadge();
  }

  function renderNotifBadge() {
    var badge = $("notifBadge");
    badge.hidden = unreadNotifications === 0;
    badge.textContent = unreadNotifications > 99 ? "99+" : String(unreadNotifications);
  }

  function renderNotifications() {
    var list = $("notifList");
    list.innerHTML = "";
    if (!notifications.length) {
      list.appendChild(el("li", "empty", "No warnings yet."));
      return;
    }
    notifications.forEach(function (record) {
      var item = el("li", "notif-item " + record.level.toLowerCase());
      item.appendChild(el("span", "notif-time", record.time || ""));
      item.appendChild(el("span", "notif-msg", record.message));
      list.appendChild(item);
    });
  }

  function redrawLogs() {
    var stream = $("logStream");
    stream.innerHTML = "";
    logBuffer.filter(logMatches).slice(-LOG_CAP).forEach(function (record) {
      stream.appendChild(logRow(record));
    });
    if ($("logAutoscroll").checked) stream.scrollTop = stream.scrollHeight;
  }

  /* ------------------------------------------------------------------ canvas */
  var canvas = $("floor");
  var ctx = canvas.getContext("2d");
  var geometry = { cell: 30, ox: 0, oy: 0, w: 0, h: 0 };
  var shelfPattern = null;
  var hazardPattern = null;

  function buildPatterns() {
    var shelf = document.createElement("canvas");
    shelf.width = shelf.height = 8;
    var sctx = shelf.getContext("2d");
    sctx.strokeStyle = COLORS.shelfLine;
    sctx.lineWidth = 1;
    sctx.beginPath();
    sctx.moveTo(-2, 10); sctx.lineTo(10, -2);
    sctx.moveTo(2, 14); sctx.lineTo(14, 2);
    sctx.stroke();
    shelfPattern = ctx.createPattern(shelf, "repeat");

    var hazard = document.createElement("canvas");
    hazard.width = hazard.height = 10;
    var hctx = hazard.getContext("2d");
    hctx.strokeStyle = "rgba(255,179,0,0.42)";
    hctx.lineWidth = 4;
    hctx.beginPath();
    hctx.moveTo(-4, 12); hctx.lineTo(12, -4);
    hctx.moveTo(1, 17); hctx.lineTo(17, 1);
    hctx.stroke();
    hazardPattern = ctx.createPattern(hazard, "repeat");
  }

  function resizeCanvas() {
    if (!state.layout) return;
    var wrap = canvas.parentElement;
    var available = Math.max(320, wrap.clientWidth - 16);
    var cell = Math.max(16, Math.floor(available / state.layout.width));
    var ratio = window.devicePixelRatio || 1;
    geometry.cell = cell;
    geometry.w = cell * state.layout.width;
    geometry.h = cell * state.layout.height;
    canvas.width = geometry.w * ratio;
    canvas.height = geometry.h * ratio;
    canvas.style.width = geometry.w + "px";
    canvas.style.height = geometry.h + "px";
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    buildPatterns();
  }

  function cellFill(type) {
    switch (type) {
      case "WALL": return COLORS.wall;
      case "SHELF": return COLORS.shelf;
      case "STORAGE": return COLORS.storage;
      case "CHARGING": return COLORS.charging;
      case "LOADING": return COLORS.loading;
      case "UNLOADING": return COLORS.unloading;
      case "PACKING": return COLORS.packing;
      case "PARKING": return COLORS.parking;
      case "RESTRICTED": return COLORS.restricted;
      default: return COLORS.floor;
    }
  }

  function drawFloor() {
    var layout = state.layout;
    var cell = geometry.cell;
    ctx.fillStyle = COLORS.floor;
    ctx.fillRect(0, 0, geometry.w, geometry.h);

    layout.cells.forEach(function (item) {
      ctx.fillStyle = cellFill(item.type);
      ctx.fillRect(item.x * cell, item.y * cell, cell, cell);
      if (item.type === "SHELF" && shelfPattern) {
        ctx.save();
        ctx.translate(item.x * cell, item.y * cell);
        ctx.fillStyle = shelfPattern;
        ctx.fillRect(0, 0, cell, cell);
        ctx.restore();
      }
      if (item.type === "RESTRICTED" && hazardPattern) {
        ctx.save();
        ctx.translate(item.x * cell, item.y * cell);
        ctx.fillStyle = hazardPattern;
        ctx.fillRect(0, 0, cell, cell);
        ctx.restore();
      }
    });

    // grid
    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (var x = 0; x <= layout.width; x++) {
      ctx.moveTo(x * cell + 0.5, 0);
      ctx.lineTo(x * cell + 0.5, geometry.h);
    }
    for (var y = 0; y <= layout.height; y++) {
      ctx.moveTo(0, y * cell + 0.5);
      ctx.lineTo(geometry.w, y * cell + 0.5);
    }
    ctx.stroke();

    // zone outlines and labels
    ctx.font = "600 " + Math.max(8, Math.round(cell * 0.3)) + "px 'Saira Condensed', sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    layout.zones.forEach(function (zone) {
      var xs = zone.cells.map(function (c) { return c.x; });
      var ys = zone.cells.map(function (c) { return c.y; });
      var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
      var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
      ctx.strokeStyle = zone.type === "RESTRICTED" ? "rgba(255,179,0,0.55)" : "rgba(49,209,196,0.24)";
      ctx.lineWidth = 1;
      ctx.strokeRect(x0 * cell + 1.5, y0 * cell + 1.5, (x1 - x0 + 1) * cell - 3, (y1 - y0 + 1) * cell - 3);
      ctx.fillStyle = "rgba(231,238,241,0.4)";
      ctx.fillText(
        zone.label.toUpperCase(),
        ((x0 + x1 + 1) / 2) * cell,
        ((y0 + y1 + 1) / 2) * cell
      );
    });
  }

  function drawRoutes() {
    var cell = geometry.cell;
    render.dash = (render.dash + 0.6) % 24;
    state.robots.forEach(function (robot) {
      if (!robot.current_path.length) return;
      var tint = robotTint(robot.id);
      var display = render.robots[robot.id] || { x: robot.position.x, y: robot.position.y };
      ctx.save();
      ctx.strokeStyle = tint;
      ctx.globalAlpha = 0.85;
      ctx.lineWidth = Math.max(2, cell * 0.11);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.setLineDash([cell * 0.34, cell * 0.3]);
      ctx.lineDashOffset = -render.dash;
      ctx.beginPath();
      ctx.moveTo((display.x + 0.5) * cell, (display.y + 0.5) * cell);
      robot.current_path.forEach(function (step) {
        ctx.lineTo((step.x + 0.5) * cell, (step.y + 0.5) * cell);
      });
      ctx.stroke();
      ctx.restore();

      // target reticle
      var target = robot.target_position;
      if (target) {
        var cx = (target.x + 0.5) * cell, cy = (target.y + 0.5) * cell;
        ctx.save();
        ctx.strokeStyle = tint;
        ctx.globalAlpha = 0.9;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(cx, cy, cell * 0.32, 0, Math.PI * 2);
        ctx.moveTo(cx - cell * 0.42, cy); ctx.lineTo(cx - cell * 0.22, cy);
        ctx.moveTo(cx + cell * 0.22, cy); ctx.lineTo(cx + cell * 0.42, cy);
        ctx.moveTo(cx, cy - cell * 0.42); ctx.lineTo(cx, cy - cell * 0.22);
        ctx.moveTo(cx, cy + cell * 0.22); ctx.lineTo(cx, cy + cell * 0.42);
        ctx.stroke();
        ctx.restore();
      }
    });
  }

  function drawBox(x, y, size, color, label) {
    var cell = geometry.cell;
    ctx.save();
    ctx.fillStyle = color;
    ctx.strokeStyle = "rgba(0,0,0,0.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.rect(x - size / 2, y - size / 2, size, size);
    ctx.fill();
    ctx.stroke();
    // packing tape
    ctx.strokeStyle = "rgba(0,0,0,0.32)";
    ctx.beginPath();
    ctx.moveTo(x, y - size / 2);
    ctx.lineTo(x, y + size / 2);
    ctx.stroke();
    if (label && cell >= 26) {
      ctx.fillStyle = "rgba(15,20,23,0.95)";
      ctx.font = "600 " + Math.round(cell * 0.24) + "px 'JetBrains Mono', monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, x, y + size * 0.02);
    }
    ctx.restore();
  }

  function drawBoxes() {
    var cell = geometry.cell;
    state.boxes.forEach(function (box) {
      if (box.status === "CARRIED" || box.status === "DELIVERING") return; // drawn on the robot
      var color = box.status === "DELIVERED" ? COLORS.boxDelivered :
        box.status === "RESERVED" || box.status === "PICKING" ? COLORS.boxCarried : COLORS.box;
      drawBox((box.position.x + 0.5) * cell, (box.position.y + 0.5) * cell, cell * 0.52, color,
        box.name.replace(/^Box-/, ""));
    });
  }

  function drawRobots() {
    var cell = geometry.cell;
    state.robots.forEach(function (robot) {
      var target = robot.position;
      var display = render.robots[robot.id];
      if (!display) {
        display = render.robots[robot.id] = { x: target.x, y: target.y };
      }
      var dx = target.x - display.x, dy = target.y - display.y;
      if (Math.abs(dx) > 2.5 || Math.abs(dy) > 2.5) {
        display.x = target.x; display.y = target.y;   // reset / teleport
      } else {
        display.x += dx * 0.24;
        display.y += dy * 0.24;
      }

      var cx = (display.x + 0.5) * cell, cy = (display.y + 0.5) * cell;
      var size = cell * 0.72;
      var tint = robotTint(robot.id);
      var halted = robot.status === "STOPPED" || robot.status === "ERROR";
      var waiting = robot.status === "WAITING";

      ctx.save();
      // shadow
      ctx.fillStyle = "rgba(0,0,0,0.45)";
      ctx.beginPath();
      ctx.ellipse(cx, cy + size * 0.42, size * 0.44, size * 0.16, 0, 0, Math.PI * 2);
      ctx.fill();

      // chassis
      ctx.translate(cx, cy);
      var rotation = { NORTH: -Math.PI / 2, EAST: 0, SOUTH: Math.PI / 2, WEST: Math.PI }[robot.orientation] || 0;
      ctx.rotate(rotation);
      ctx.fillStyle = halted ? "#3a2226" : "#1d272c";
      ctx.strokeStyle = halted ? COLORS.fault : waiting ? COLORS.warn : tint;
      ctx.lineWidth = Math.max(1.6, cell * 0.06);
      var r = size * 0.22;
      ctx.beginPath();
      ctx.moveTo(-size / 2 + r, -size / 2);
      ctx.arcTo(size / 2, -size / 2, size / 2, size / 2, r);
      ctx.arcTo(size / 2, size / 2, -size / 2, size / 2, r);
      ctx.arcTo(-size / 2, size / 2, -size / 2, -size / 2, r);
      ctx.arcTo(-size / 2, -size / 2, size / 2, -size / 2, r);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();

      // heading notch
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(size * 0.5, 0);
      ctx.lineTo(size * 0.22, -size * 0.18);
      ctx.lineTo(size * 0.22, size * 0.18);
      ctx.closePath();
      ctx.fill();
      ctx.restore();

      // carried box rides on the chassis
      if (robot.carrying_box) {
        drawBox(cx, cy - size * 0.1, size * 0.5, COLORS.boxCarried,
          boxLabel(robot.carrying_box).replace(/^Box-/, ""));
      }

      // name + battery gauge
      if (cell >= 22) {
        ctx.save();
        ctx.font = "700 " + Math.round(cell * 0.28) + "px 'Saira Condensed', sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "alphabetic";
        ctx.fillStyle = COLORS.ink;
        ctx.fillText(robot.name.toUpperCase(), cx, cy - size * 0.62);

        var gw = size * 0.9, gh = Math.max(2.5, cell * 0.09);
        var gx = cx - gw / 2, gy = cy + size * 0.56;
        ctx.fillStyle = "rgba(0,0,0,0.6)";
        ctx.fillRect(gx, gy, gw, gh);
        ctx.fillStyle = robot.battery <= 8 ? COLORS.fault : robot.battery <= 20 ? COLORS.warn : COLORS.ok;
        ctx.fillRect(gx, gy, gw * (robot.battery / 100), gh);

        ctx.font = "500 " + Math.round(cell * 0.24) + "px 'JetBrains Mono', monospace";
        ctx.fillStyle = waiting ? COLORS.warn : halted ? COLORS.fault : COLORS.muted;
        ctx.fillText(robot.status, cx, gy + gh + Math.round(cell * 0.3));
        ctx.restore();
      }
    });
  }

  function draw() {
    if (state.layout) {
      drawFloor();
      drawRoutes();
      drawBoxes();
      drawRobots();
    }
    requestAnimationFrame(draw);
  }

  function renderLegend() {
    var node = $("legend");
    node.innerHTML = "";
    LEGEND.forEach(function (item) {
      var span = el("span");
      var swatch = el("i");
      swatch.style.background = item[1];
      span.appendChild(swatch);
      span.appendChild(document.createTextNode(item[0]));
      node.appendChild(span);
    });
  }

  /* --------------------------------------------------------------- ingestion */
  var domDirty = false;
  function applyState(snapshot) {
    if (snapshot.warehouse) {
      var changed = !state.layout ||
        state.layout.width !== snapshot.warehouse.width ||
        state.layout.height !== snapshot.warehouse.height;
      state.layout = snapshot.warehouse;
      if (changed) resizeCanvas();
    }
    if (snapshot.config) state.config = snapshot.config;
    state.environment = snapshot.environment || state.environment;
    state.robots = snapshot.robots || [];
    state.boxes = snapshot.boxes || [];
    state.agents = snapshot.agents || [];
    state.operators = snapshot.operators || [];
    state.tasks = snapshot.tasks || [];
    state.statistics = snapshot.statistics || {};
    state.robotStatistics = snapshot.robot_statistics || [];
    state.options = snapshot.options || state.options;
    if (snapshot.ci) state.ci = snapshot.ci;
    domDirty = true;
  }

  function flushDom() {
    if (domDirty) {
      domDirty = false;
      renderHeader();
      renderStats();
      renderTasks();
      renderRobots();
      renderBoxes();
      renderAgents();
      renderOperators();
      renderTimeline();
      renderCi();
      syncOptions();
      syncTaskFilter();
      var speed = $("speedSelect");
      if (speed.value !== String(state.environment.simulation_speed)) {
        var wanted = String(state.environment.simulation_speed);
        if (speed.querySelector('option[value="' + wanted + '"]')) speed.value = wanted;
      }
    }
    setTimeout(flushDom, 300);
  }

  function connect() {
    var source = new EventSource("/api/stream");
    source.addEventListener("state", function (message) {
      applyState(JSON.parse(message.data));
    });
    source.addEventListener("log", function (message) {
      appendLog(JSON.parse(message.data));
    });
    source.addEventListener("ci", function (message) {
      state.ci = JSON.parse(message.data);
      renderCi();
    });
    source.addEventListener("agent-chat", function (message) {
      onAgentChatProgress(JSON.parse(message.data));
    });
    source.onerror = function () {
      $("statusLamp").className = "lamp stopped";
      $("simStatus").textContent = "reconnecting";
    };
  }

  /* ------------------------------------------------------------------ wiring */
  /* ---------------------------------------------------------------- chat
     The chat agent (backend/agent_chat.py) is a real tool-use loop, not
     the one-shot narration used elsewhere — it decides what to look up
     or do and calls the same backend any other client would. Whatever it
     creates/cancels shows up on the dashboard the normal way (the next
     SSE state push), no special wiring needed here beyond the chat log
     itself. */
  function chatEscapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function chatInlineMarkdown(escaped) {
    // Input is already HTML-escaped — these only ever introduce the
    // specific tags below, never raw model output.
    return escaped
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,!?]|$)/g, "$1<i>$2</i>");
  }

  /* A small, safe markdown-lite renderer for the chat agent's replies —
     bold/italic/inline-code, bullet and numbered lists, paragraphs. Not
     a general markdown parser (no tables/links/nested lists), just
     enough for the plain, structured prose the agent actually writes. */
  function renderChatMarkdown(raw) {
    var lines = String(raw || "").split(/\r?\n/);
    var blocks = [];
    var list = null; // {tag: "ul"|"ol", items: [...]}

    function flushList() {
      if (!list) return;
      blocks.push("<" + list.tag + ">" + list.items.map(function (i) { return "<li>" + i + "</li>"; }).join("") + "</" + list.tag + ">");
      list = null;
    }

    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (!trimmed) { flushList(); return; }
      var bullet = trimmed.match(/^[-*]\s+(.*)$/);
      var numbered = trimmed.match(/^\d+[.)]\s+(.*)$/);
      if (bullet) {
        if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
        list.items.push(chatInlineMarkdown(chatEscapeHtml(bullet[1])));
        return;
      }
      if (numbered) {
        if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
        list.items.push(chatInlineMarkdown(chatEscapeHtml(numbered[1])));
        return;
      }
      flushList();
      blocks.push("<p>" + chatInlineMarkdown(chatEscapeHtml(trimmed)) + "</p>");
    });
    flushList();
    return blocks.join("");
  }

  function chatScrollToBottom() {
    var log = $("chatLog");
    log.scrollTop = log.scrollHeight;
  }

  function renderChatEmptyState() {
    var log = $("chatLog");
    if (!log.children.length) {
      log.appendChild(el("p", "empty", "Ask a question, or tell it what to do."));
    }
  }

  /* {tool, args, result} -> ["ok"|"err", "label"] — shared by the final
     actions list and the live progress list below, so a step looks
     identical whether it's still streaming in or already settled. */
  function chatActionLabel(action) {
    var ok = !(action.result && action.result.error);
    var label = action.tool || "?";
    if (action.result) {
      if (ok && action.result.task_id) label += ": " + action.result.task_id + " (" + (action.result.status || "") + ")";
      else if (!ok) label += ": " + action.result.error;
    }
    return [ok ? "ok" : "err", label];
  }

  function appendChatMessage(role, text, actions) {
    var log = $("chatLog");
    var empty = log.querySelector(".empty");
    if (empty) empty.remove();
    var msg = el("div", "chat-msg " + role);
    var bubble = el("div", "bubble");
    if (role === "assistant") {
      bubble.innerHTML = renderChatMarkdown(text);
    } else {
      bubble.textContent = text; // user / error — plain text, no markdown to render
    }
    msg.appendChild(bubble);
    if (actions && actions.length) {
      var list = el("ul", "chat-actions");
      actions.forEach(function (action) {
        var parts = chatActionLabel(action);
        list.appendChild(el("li", parts[0], parts[1]));
      });
      msg.appendChild(list);
    }
    log.appendChild(msg);
    chatScrollToBottom();
    return msg;
  }

  /* The in-flight turn's bubble — a live status line plus a list that
     grows one entry per completed tool call, driven by onAgentChatProgress
     below (SSE event "agent-chat") instead of sitting on one static
     "thinking…" until the whole multi-step command finishes. */
  function appendPendingChatMessage() {
    var log = $("chatLog");
    var empty = log.querySelector(".empty");
    if (empty) empty.remove();
    var msg = el("div", "chat-msg assistant pending");
    var bubble = el("div", "bubble");
    bubble.appendChild(el("p", "chat-status", "Thinking…"));
    bubble.appendChild(el("ul", "chat-actions live"));
    msg.appendChild(bubble);
    log.appendChild(msg);
    chatScrollToBottom();
    return msg;
  }

  function onAgentChatProgress(progress) {
    if (!chatBusy) return; // no turn in flight (a stray/late event) — nothing to attach it to
    var pending = $("chatLog").querySelector(".chat-msg.pending");
    if (!pending) return;
    var status = pending.querySelector(".chat-status");
    var list = pending.querySelector(".chat-actions.live");
    if (!status || !list) return;
    if (progress.phase === "thinking") {
      status.textContent = progress.iteration > 1 ? "Thinking… (step " + progress.iteration + ")" : "Thinking…";
    } else if (progress.phase === "action") {
      var parts = chatActionLabel(progress);
      list.appendChild(el("li", parts[0], parts[1]));
      status.textContent = "Working…";
    }
    chatScrollToBottom();
  }

  function setChatBadge(n) {
    chatUnread = n;
    var badge = $("chatLauncherBadge");
    badge.hidden = chatUnread === 0;
    badge.textContent = chatUnread > 9 ? "9+" : String(chatUnread);
  }

  function openChat() {
    chatOpen = true;
    $("chatPanel").hidden = false;
    setChatBadge(0);
    renderChatEmptyState();
    $("chatInput").focus();
  }

  function closeChat() {
    chatOpen = false;
    $("chatPanel").hidden = true;
  }

  function sendChatMessage(text) {
    if (chatBusy) return;
    chatBusy = true;
    $("chatSendBtn").disabled = true;
    appendChatMessage("user", text);
    var pending = appendPendingChatMessage();

    api("/api/agent/chat", { method: "POST", body: { message: text, history: chatHistory } })
      .then(function (result) {
        pending.remove();
        appendChatMessage("assistant", result.reply, result.actions);
        chatHistory.push({ role: "user", content: text });
        chatHistory.push({ role: "assistant", content: result.reply });
        if (chatHistory.length > 16) chatHistory = chatHistory.slice(-16);
        if (!chatOpen) setChatBadge(chatUnread + 1);
      })
      .catch(function (error) {
        pending.remove();
        appendChatMessage("error", error.message);
        if (!chatOpen) setChatBadge(chatUnread + 1);
      })
      .then(function () {
        chatBusy = false;
        $("chatSendBtn").disabled = false;
      });
  }

  function wire() {
    renderLegend();

    document.querySelectorAll("[data-sim]").forEach(function (button) {
      button.addEventListener("click", function () {
        var action = button.dataset.sim;
        var path = action === "reset" ? "/api/simulation/reset" : "/api/simulation/" + action;
        api(path, { method: "POST", body: action === "reset" ? { demo_tasks: true } : undefined })
          .then(function (body) {
            if (body.state) applyState(body.state);
            if (action === "reset") { logBuffer = []; redrawLogs(); selectedTaskId = null; }
            toast("Simulation " + action, "good");
          })
          .catch(function (error) { toast(error.message, "bad"); });
      });
    });

    document.querySelectorAll("[data-state]").forEach(function (button) {
      button.addEventListener("click", function () {
        var action = button.dataset.state;
        api("/api/state/" + action, { method: "POST" })
          .then(function (body) {
            if (body.state) applyState(body.state);
            toast(action === "save" ? "State saved to data/warehouse_state.json" : "State loaded", "good");
          })
          .catch(function (error) { toast(error.message, "bad"); });
      });
    });

    $("collisionRiskSlider").addEventListener("change", function () {
      var risk = Number(this.value) / 100;
      var note = $("policyFormNote");
      api("/api/policies/collision-risk", { method: "POST", body: { risk: risk } })
        .then(function () {
          $("collisionRiskValue").textContent = Math.round(risk * 100) + "%";
          note.className = "form-note ok";
          note.textContent = risk > 0
            ? "Collision risk set to " + Math.round(risk * 100) + "% — robots may now actually collide while working"
            : "Collision risk off — the traffic system avoids every conflict again";
        })
        .catch(function (error) {
          note.className = "form-note bad";
          note.textContent = error.message;
        });
    });
    $("collisionRiskSlider").addEventListener("input", function () {
      $("collisionRiskValue").textContent = this.value + "%";
    });

    $("falseSuccessRiskSlider").addEventListener("change", function () {
      var risk = Number(this.value) / 100;
      var note = $("policyFormNote");
      api("/api/policies/false-success-risk", { method: "POST", body: { risk: risk } })
        .then(function () {
          $("falseSuccessRiskValue").textContent = Math.round(risk * 100) + "%";
          note.className = "form-note ok";
          note.textContent = risk > 0
            ? "False success risk set to " + Math.round(risk * 100) + "% — some deliveries will now report success without actually happening"
            : "False success risk off — every completed delivery really landed the box";
        })
        .catch(function (error) {
          note.className = "form-note bad";
          note.textContent = error.message;
        });
    });
    $("falseSuccessRiskSlider").addEventListener("input", function () {
      $("falseSuccessRiskValue").textContent = this.value + "%";
    });

    $("estopBtn").addEventListener("click", function () {
      api("/api/simulation/emergency-stop", { method: "POST" })
        .then(function () { toast("Emergency stop engaged — every robot is halted", "bad"); })
        .catch(function (error) { toast(error.message, "bad"); });
    });

    $("resumeBtn").addEventListener("click", function () {
      api("/api/simulation/resume", { method: "POST" })
        .then(function () { toast("Robots resuming their tasks", "good"); })
        .catch(function (error) { toast(error.message, "bad"); });
    });

    $("speedSelect").addEventListener("change", function () {
      api("/api/simulation/speed", { method: "POST", body: { speed: Number(this.value) } })
        .catch(function (error) { toast(error.message, "bad"); });
    });

    $("taskType").addEventListener("change", applyTaskFieldVisibility);

    $("taskForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var note = $("taskFormNote");
      var allowed = TASK_FIELDS[$("taskType").value] || [];
      var body = { type: $("taskType").value, robot_id: $("taskRobot").value };
      if (allowed.indexOf("box") !== -1) body.box_id = $("taskBox").value;
      if (allowed.indexOf("box_ids") !== -1) {
        body.box_ids = Array.prototype.slice.call($("taskBoxes").selectedOptions)
          .map(function (o) { return o.value; });
      }
      if (allowed.indexOf("source") !== -1 && $("taskSource").value) body.source = $("taskSource").value;
      if (allowed.indexOf("destination") !== -1) body.destination = $("taskDestination").value;
      if (allowed.indexOf("priority") !== -1) body.priority = $("taskPriority").value;
      if (allowed.indexOf("agent") !== -1) body.agent_id = $("taskAgent").value;
      if (allowed.indexOf("operator") !== -1) {
        body.operator_id = $("taskOperator").value;
        if ($("taskDualSignoff").checked) {
          body.dual_signoff = true;
          if ($("taskSecondOperator").value !== "AUTO") body.second_operator_id = $("taskSecondOperator").value;
        }
      }

      api("/api/tasks", { method: "POST", body: body })
        .then(function (result) {
          note.className = "form-note ok";
          note.textContent = result.task.id + " accepted — " + result.task.status.toLowerCase();
          selectedTaskId = result.task.id;
          toast(result.task.id + " assigned", "good");
        })
        .catch(function (error) {
          note.className = "form-note bad";
          note.textContent = error.message;
          toast(error.message, "bad");
        });
    });

    $("robotForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = new FormData(this);
      var note = $("robotFormNote");
      api("/api/robots", {
        method: "POST",
        body: {
          name: form.get("name"),
          robot_class: form.get("robot_class"),
          speed: form.get("speed") || null,
          x: form.get("x"),
          y: form.get("y")
        }
      }).then(function (result) {
        note.className = "form-note ok";
        note.textContent = result.robot.name + " joined the floor at " + pos(result.robot.position);
        optionsSignature = "";
      }).catch(function (error) {
        note.className = "form-note bad";
        note.textContent = error.message;
      });
    });

    $("boxForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = new FormData(this);
      var note = $("boxFormNote");
      api("/api/boxes", {
        method: "POST",
        body: {
          name: form.get("name"),
          weight: form.get("weight"),
          x: form.get("x"),
          y: form.get("y"),
          destination: form.get("destination") || null
        }
      }).then(function (result) {
        note.className = "form-note ok";
        note.textContent = result.box.name + " placed at " + pos(result.box.position);
        optionsSignature = "";
      }).catch(function (error) {
        note.className = "form-note bad";
        note.textContent = error.message;
      });
    });

    $("agentForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = new FormData(this);
      var note = $("agentFormNote");
      api("/api/agents", {
        method: "POST",
        body: {
          name: form.get("name"),
          model_version: form.get("model_version") || null
        }
      }).then(function (result) {
        note.className = "form-note ok";
        note.textContent = result.agent.name + " added (" + result.agent.model_version + ")";
        optionsSignature = "";
      }).catch(function (error) {
        note.className = "form-note bad";
        note.textContent = error.message;
      });
    });

    $("operatorForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = new FormData(this);
      var note = $("operatorFormNote");
      var certs = (form.get("certifications") || "")
        .split(",").map(function (c) { return c.trim(); }).filter(Boolean);
      api("/api/operators", {
        method: "POST",
        body: {
          name: form.get("name"),
          role: form.get("role") || null,
          certifications: certs,
          shift_start_hour: form.get("shift_start_hour") || null,
          shift_end_hour: form.get("shift_end_hour") || null
        }
      }).then(function (result) {
        note.className = "form-note ok";
        var shiftNote = result.operator.shift_start_hour !== null
          ? " · shift " + result.operator.shift_start_hour + "-" + result.operator.shift_end_hour
          : "";
        note.textContent = result.operator.name + " added (" + (result.operator.certifications.join(", ") || "no certs") + ")" + shiftNote;
        optionsSignature = "";
      }).catch(function (error) {
        note.className = "form-note bad";
        note.textContent = error.message;
      });
    });

    $("ciRunBtn").addEventListener("click", function () {
      var status = $("ciStatus");
      status.textContent = "running";
      status.className = "ci-status running";
      this.disabled = true;
      var button = this;
      api("/api/ci/run", { method: "POST" })
        .then(function (summary) {
          state.ci = summary;
          renderCi();
          toast("CI " + summary.pipeline + " " + summary.status +
            " (" + summary.passed + "/" + summary.total + ")",
            summary.status === "PASSED" ? "good" : "bad");
        })
        .catch(function (error) { toast(error.message, "bad"); })
        .then(function () { button.disabled = false; });
    });

    $("scheduleForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = new FormData(this);
      var note = $("scheduleFormNote");
      api("/api/schedules", {
        method: "POST",
        body: {
          payload: { type: form.get("type") },
          interval_seconds: Number(form.get("interval_seconds")) || 30
        }
      }).then(function () {
        note.className = "form-note ok";
        note.textContent = "Schedule added";
        refreshSchedules();
      }).catch(function (error) {
        note.className = "form-note bad";
        note.textContent = error.message;
      });
    });

    $("notifBell").addEventListener("click", function () {
      var panel = $("notifPanel");
      panel.hidden = !panel.hidden;
      if (!panel.hidden) {
        unreadNotifications = 0;
        renderNotifBadge();
      }
    });
    $("notifClearBtn").addEventListener("click", function () {
      notifications = [];
      unreadNotifications = 0;
      renderNotifications();
    });
    document.addEventListener("click", function (event) {
      var wrap = document.querySelector(".notif-wrap");
      if (wrap && !wrap.contains(event.target)) $("notifPanel").hidden = true;
    });

    $("decisionForm").addEventListener("submit", function (event) {
      event.preventDefault();
      runDecisionSearch();
    });

    $("policyReloadBtn").addEventListener("click", function () {
      var note = $("policyFormNote");
      api("/api/policies/reload", { method: "POST" })
        .then(function (policy) {
          renderPolicies(policy);
          note.className = "form-note ok";
          note.textContent = "Reloaded from " + (policy.source_file || "built-in defaults");
        })
        .catch(function (error) {
          note.className = "form-note bad";
          note.textContent = error.message;
        });
    });

    $("policyLlmToggle").addEventListener("change", function () {
      var checked = this.checked;
      var note = $("policyFormNote");
      api("/api/policies/llm-narration", { method: "POST", body: { enabled: checked } })
        .then(function () {
          note.className = "form-note ok";
          note.textContent = "Live AI narration " + (checked ? "enabled" : "disabled") +
            " (in-memory — not saved to policies.yaml)";
        })
        .catch(function (error) {
          this.checked = !checked;
          note.className = "form-note bad";
          note.textContent = error.message;
        }.bind(this));
    });

    ["logLevel", "logCategory", "logRobot", "logTask"].forEach(function (id) {
      $(id).addEventListener("change", redrawLogs);
    });
    $("logSearch").addEventListener("input", redrawLogs);
    $("logClearBtn").addEventListener("click", function () {
      api("/api/logs/clear", { method: "POST" }).then(function () {
        logBuffer = [];
        redrawLogs();
        toast("Log history cleared", "good");
      }).catch(function (error) { toast(error.message, "bad"); });
    });
    $("logExportTxt").addEventListener("click", function () {
      window.location.href = "/api/logs/export?format=txt";
    });
    $("logExportJson").addEventListener("click", function () {
      window.location.href = "/api/logs/export?format=json";
    });

    $("modalClose").addEventListener("click", closeModal);
    $("taskModal").addEventListener("click", function (event) {
      if (event.target === this) closeModal();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { closeModal(); closeChat(); }
    });

    $("chatLauncher").addEventListener("click", function () {
      if (chatOpen) closeChat(); else openChat();
    });
    $("chatCloseBtn").addEventListener("click", closeChat);
    $("chatForm").addEventListener("submit", function (event) {
      event.preventDefault();
      var input = $("chatInput");
      var text = input.value.trim();
      if (!text || chatBusy) return;
      input.value = "";
      sendChatMessage(text);
    });

    window.addEventListener("resize", function () { resizeCanvas(); });
  }

  /* -------------------------------------------------------------------- boot */
  function boot() {
    wire();
    api("/api/state")
      .then(function (snapshot) {
        applyState(snapshot);
        resizeCanvas();
        return api("/api/logs?limit=300");
      })
      .then(function (body) {
        logBuffer = body.logs || [];
        redrawLogs();
      })
      .catch(function (error) {
        toast("Could not reach the backend: " + error.message, "bad");
      })
      .then(function () {
        connect();
        flushDom();
        requestAnimationFrame(draw);
        refreshSchedules();
        refreshFleetLoad();
        refreshTrends();
        refreshPolicies();
        setInterval(function () {
          refreshSchedules();
          refreshFleetLoad();
          refreshTrends();
        }, 5000);
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
