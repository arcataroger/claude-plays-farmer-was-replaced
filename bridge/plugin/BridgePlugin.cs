// TFWR Bridge: a loopback control socket for The Farmer Was Replaced.
//
// It does only what a player's hands and eyes do: open code windows, press
// Execute/Stop, read the output panel and take screenshots. It never reads
// world-generation or RNG state. Only `hover` touches the grid: it mirrors the
// game's own mouse raycast and returns the tooltip a player would see.
// bridge/README.md ("Fair-play audit") lists every game value the plugin reads.
//
// Protocol: one request per line, fields separated by tabs. Code travels as
// base64 (UTF-8). Each request gets one JSON line back.

using System;
using System.Linq;
using System.Collections.Generic;
using System.Collections;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using BepInEx;
using BepInEx.Configuration;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.SceneManagement;

namespace TfwrBridge
{
    [BepInPlugin(Guid, "TFWR Bridge", Version)]
    public class BridgePlugin : BaseUnityPlugin
    {
        public const string Guid = "dev.tfwr.bridge";
        public const string Version = "0.2.0";

        private class Job
        {
            public string[] Args;
            public string Result;
            public bool FromClient;  // only client requests go to the audit log
            public readonly ManualResetEventSlim Done = new ManualResetEventSlim(false);
            public static Action<Job> Finished;

            public void Finish(string result)
            {
                Result = result;
                if (FromClient) Finished?.Invoke(this);
                Done.Set();
            }
        }

        private readonly ConcurrentQueue<Job> jobs = new ConcurrentQueue<Job>();
        private ConfigEntry<int> port;
        private TcpListener listener;
        private volatile bool serving;
        private Workspace workspace;

        // Farm-centering runs over several frames, so it keeps its own state.
        private Job centerJob;
        private Vector2 centerTarget;
        private int centerFrames;

        private const BindingFlags Private = BindingFlags.Instance | BindingFlags.NonPublic;
        private static readonly FieldInfo ErrorStringField = typeof(CodeWindow).GetField("errorString", Private);
        // The game's file watcher reloads .py files into their windows. We inject code
        // directly, so we turn it off while the bridge drives; otherwise a Save's own
        // file writes come back a moment later and replace the injected code.
        private static readonly FieldInfo WatcherField = typeof(Saver).GetField("watcher", BindingFlags.Static | BindingFlags.NonPublic);
        private static readonly FieldInfo PendingField = typeof(Saver).GetField("pendingCodeChanges", BindingFlags.Static | BindingFlags.NonPublic);
        private static readonly FieldInfo PendingLockField = typeof(Saver).GetField("pendingCodeChangesLock", BindingFlags.Static | BindingFlags.NonPublic);
        private static readonly FieldInfo SimField = typeof(MainSim).GetField("sim", Private);
        private static readonly FieldInfo LockField = typeof(MainSim).GetField("lockSimulation", Private | BindingFlags.Static) ?? typeof(MainSim).GetField("lockSimulation", BindingFlags.Static | BindingFlags.NonPublic);
        private static readonly MethodInfo GetHoveredTooltipMethod = typeof(MainSim).GetMethod("GetHoveredTooltip", Private);
        private static readonly FieldInfo TransitionTargetField = typeof(Workspace).GetField("transitionTarget", Private);
        private static readonly FieldInfo ContentRectField = typeof(CameraController).GetField("contentRect", Private);
        private static readonly FieldInfo SceneScalerField = typeof(CameraController).GetField("sceneScaler", Private);
        private static readonly FieldInfo MinZoomField = typeof(Workspace).GetField("minCanvasScaleFactor", Private);
        private static readonly FieldInfo MaxZoomField = typeof(Workspace).GetField("maxCanvasScaleFactor", Private);

        // Steam's build id for this game, read from appmanifest_2060160.acf in the
        // steamapps folder above the game's data folder; null when not found.
        private static string SteamBuildId()
        {
            try
            {
                var dir = new DirectoryInfo(Application.dataPath);
                while (dir != null && !string.Equals(dir.Name, "steamapps", StringComparison.OrdinalIgnoreCase)) dir = dir.Parent;
                if (dir == null) return null;
                string acf = Path.Combine(dir.FullName, "appmanifest_2060160.acf");
                if (!File.Exists(acf)) return null;
                var m = System.Text.RegularExpressions.Regex.Match(File.ReadAllText(acf), "\"buildid\"\\s+\"(\\d+)\"");
                return m.Success ? m.Groups[1].Value : null;
            }
            catch (Exception) { return null; }
        }

        private void Awake()
        {
            port = Config.Bind("Server", "Port", 7777, "Loopback TCP port for the bridge.");
            dataDir = Application.persistentDataPath;
            Job.Finished = AuditCommand;
            // Record the game build: Steam can update the game mid-project (2026-11-03),
            // and benchmark runs must say which build they played.
            Audit("plugin_start", Kv("plugin", Version), Kv("unity", Application.unityVersion),
                  Kv("platform", Application.platform.ToString()), Kv("game_version", Application.version),
                  Kv("steam_buildid", SteamBuildId()));
            serving = true;
            var thread = new Thread(Serve) { IsBackground = true, Name = "TfwrBridge" };
            thread.Start();
            Logger.LogInfo($"TFWR Bridge {Version} listening on 127.0.0.1:{port.Value}");
        }

        private void OnDestroy()
        {
            serving = false;
            listener?.Stop();
        }

        // ---------------------------------------------------------------- network thread

        private void Serve()
        {
            try
            {
                listener = new TcpListener(IPAddress.Loopback, port.Value);
                listener.Start();
            }
            catch (Exception e)
            {
                Logger.LogError($"Bridge could not listen on port {port.Value}: {e.Message}");
                return;
            }
            while (serving)
            {
                TcpClient client;
                try
                {
                    client = listener.AcceptTcpClient();
                }
                catch (Exception)
                {
                    if (!serving) return;
                    continue;
                }
                ThreadPool.QueueUserWorkItem(_ => Handle(client));
            }
        }

        private void Handle(TcpClient client)
        {
            try
            {
                using (client)
                {
                    var stream = client.GetStream();
                    var utf8 = new UTF8Encoding(false);
                    var reader = new StreamReader(stream, utf8);
                    var writer = new StreamWriter(stream, utf8) { NewLine = "\n", AutoFlush = true };
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        line = line.TrimEnd('\r');
                        if (line.Length == 0) continue;
                        var job = new Job { Args = line.Split('\t'), FromClient = true };
                        jobs.Enqueue(job);
                        // Unity only ticks while the game runs; a paused game times out here.
                        writer.WriteLine(job.Done.Wait(TimeSpan.FromSeconds(30))
                            ? job.Result
                            : Err("timed out waiting for the game's main thread"));
                    }
                }
            }
            catch (IOException)
            {
                // The client hung up.
            }
            catch (Exception e)
            {
                Logger.LogWarning($"Bridge connection error: {e.Message}");
            }
        }

        // ---------------------------------------------------------------- main thread

        private void Update()
        {
            TrackGoal();
            while (jobs.TryDequeue(out var job))
            {
                try
                {
                    Dispatch(job);
                }
                catch (Exception e)
                {
                    job.Finish(Err(e.GetType().Name + ": " + e.Message));
                }
            }
        }

        // LateUpdate runs after CameraController.Update, so the farm's screen position is current.
        // Run bookkeeping: the client waits for runs_finished to reach its run_id, so it
        // needs no marker in the player's code (quick_print needs the Debug unlock).
        private int runsStarted, runsFinished, runStartFrame;
        private bool runPending;
        private string runName;
        private bool ffUntilIdle;
        private int ffFrame;

        // Player speed buttons (bottom right): 1x, a fast-forward that cycles 2x..128x, and
        // Auto. While the player holds manual speed, we pin the clock every frame and ignore
        // the client's fast_forward calls; Auto hands the clock back to the client's ramp.
        private bool manualSpeed;
        private double manualFactor = 1.0;
        private GUIStyle speedStyle;
        private float speedPanelW;

        // Watchdog: the plugin, not the client, ends a bridge run that runs too long or stops
        // making progress. A client that dies or forgets its checks can't leave a run going for
        // hours.
        // `watchdog limit S stall S items a,b` sets the next run's limits. Run() latches them and
        // resets the next run's values to the defaults, so a forgotten call gets the defaults.
        // A change while a run is going applies to that run too.
        // Every minute the run writes a check-in line to claude_watchdog.log in the data folder.
        private const float DefaultLimit = 1200f, DefaultStall = 300f, CheckEvery = 60f;
        private float wdLimit = DefaultLimit, wdStall = DefaultStall;          // the current run's
        private float nextLimit = DefaultLimit, nextStall = DefaultStall;      // the next run's
        // Goal items: when any is set, only their rise counts as progress. A client sets them
        // with `watchdog items`, and `wait` adds its goal item (run --goal and farm send one).
        private readonly HashSet<int> wdItems = new HashSet<int>(), nextItems = new HashSet<int>();
        private int wdGoal = -1, nextGoal = -1;
        private float wdStart, wdNextCheck, wdProgressAt, wdNextSample;
        private double wdProgress;
        private string wdStopped = "";  // why the watchdog ended the last run ("" = it didn't)

        // A caption at the bottom centre that says what the agent is doing.
        // Plugin actions set it themselves; the client sets the high-level step with `say`.
        private string caption = "";
        private bool captionFromClient;  // a `say` right before `run` keeps its own text
        private float captionUntil;
        private GUIStyle captionStyle;
        private Texture2D captionBack;

        private void Say(string text, float seconds = 0f)
        {
            caption = text ?? "";
            captionUntil = seconds > 0f ? Time.unscaledTime + seconds : float.MaxValue;
        }

        // A live status line at the bottom edge, under the caption: what we wait for, how long so far, and the
        // speed. It redraws every frame, so the clock and speed stay current on their own.
        // Captions describe the step; the pill owns the speed, so the ramp never hides a caption.
        private string waitText = "";
        private float waitStart, waitTimeout;
        private float warningClearedAt = -1f;
        private int goalItem = -1;   // the item we wait for (-1 = none), as the top bar counts it
        private double goalTarget, goalStart;
        // The estimate runs on farm seconds (real seconds times the game speed), so the
        // fast-forward ramp doesn't skew it. Payouts can come in lumps,
        // so we take the rate at the last change and count down from there.
        private double farmElapsed, lastChangeFarm, lastHave;
        private float columnRight;   // the code column's right edge in pixels (0 = not arranged)
        private bool waitFromClient;  // a `wait` right before `run` keeps its own text

        private void Wait(string text, float timeout = 0f, string item = null, double target = 0)
        {
            waitText = text ?? "";
            waitStart = Time.unscaledTime;
            waitTimeout = timeout;
            int id = string.IsNullOrEmpty(item) ? -1 : StringIds.GetItemId(item.ToLowerInvariant());
            // The watchdog keeps its own copy: the status line clears its goal when a run ends.
            if (id >= 0) { nextGoal = id; if (runPending) wdGoal = id; }
            // `farm` re-sends the same goal with every chunk; keep its rate history.
            if (id >= 0 && id == goalItem && target == goalTarget) return;
            goalItem = id;
            goalTarget = target;
            goalStart = goalItem >= 0 ? MainSim.Inst.GetNumItem(goalItem) : 0;
            farmElapsed = lastChangeFarm = 0;
            lastHave = goalStart;
        }

        private void TrackGoal()
        {
            if (goalItem < 0 || MainSim.Inst == null) return;
            farmElapsed += Time.unscaledDeltaTime * MainSim.Inst.TimeFactor;
            double have = MainSim.Inst.GetNumItem(goalItem);
            if (have != lastHave) { lastHave = have; lastChangeFarm = farmElapsed; }
        }

        // Real seconds until the goal, or -1 before the first gain.
        private double SecondsLeft(double have)
        {
            double gained = lastHave - goalStart;
            if (gained <= 0 || lastChangeFarm < 1) return -1;
            double farmLeft = (goalTarget - have) / (gained / lastChangeFarm) - (farmElapsed - lastChangeFarm);
            return Math.Max(0, farmLeft) / Math.Max(1.0, MainSim.Inst.TimeFactor);
        }

        // 1234567 -> 1.23M, as the top bar writes it.
        private static string Short(double n)
        {
            if (n >= 1e9) return (n / 1e9).ToString("0.##") + "B";
            if (n >= 1e6) return (n / 1e6).ToString("0.##") + "M";
            if (n >= 1e4) return (n / 1e3).ToString("0.#") + "k";
            return n.ToString("#,0");
        }

        private static string Clock(float seconds)
        {
            int s = Mathf.Max(0, Mathf.FloorToInt(seconds));
            return (s / 60) + ":" + (s % 60).ToString("00");
        }

        private string StatusLine()
        {
            var parts = new List<string>();
            if (!string.IsNullOrEmpty(waitText))
            {
                // With a goal, show progress on the item itself and a real-time estimate
                // from the rate so far. Without one, show the real clock against the timeout.
                float t = Time.unscaledTime - waitStart;
                parts.Add("Waiting for " + waitText);
                if (goalItem >= 0)
                {
                    double have = MainSim.Inst.GetNumItem(goalItem), left = SecondsLeft(have);
                    string name = StringIds.GetItemName(goalItem).Replace("_", " ");
                    // Percent of the target, matching the "have of target" text beside it.
                    // goalStart only feeds the time estimate.
                    double pct = goalTarget > 0 ? 100 * have / goalTarget : 100;
                    parts.Add(char.ToUpper(name[0]) + name.Substring(1) + " " + Short(have) + " of " + Short(goalTarget)
                              + " (" + Mathf.Clamp((float)pct, 0f, 100f).ToString("0") + "%)");
                    if (have >= goalTarget) parts.Add("reached");
                    else if (left >= 0 && t > 3f) parts.Add("about " + Clock((float)left) + " left");
                    else parts.Add(Clock(t));
                }
                else parts.Add(waitTimeout > 0f ? Clock(t) + " of " + Clock(waitTimeout) : Clock(t));
            }
            if (runPending && wdLimit > 0) parts.Add("stops at " + Clock(wdLimit));
            double speed = MainSim.Inst != null ? MainSim.Inst.TimeFactor : 1.0;
            if (manualSpeed) parts.Add("speed " + speed.ToString("0") + "x (manual)");
            else if (speed > 1.01) parts.Add("speed " + speed.ToString("0") + "x");
            return string.Join("   \u00b7   ", parts.ToArray());
        }

        private void OnGUI()
        {
            if (captionStyle == null)
            {
                captionBack = new Texture2D(1, 1);
                captionBack.SetPixel(0, 0, new Color(0.08f, 0.09f, 0.12f, 0.82f));
                captionBack.Apply();
                captionStyle = new GUIStyle(GUI.skin.label)
                {
                    alignment = TextAnchor.MiddleCenter,
                    wordWrap = true,
                    richText = true,
                };
                captionStyle.normal.background = captionBack;
                captionStyle.normal.textColor = new Color(0.95f, 0.93f, 0.85f);
            }
            SpeedButtons();
            // The resource bar fills the top, so both stack at the bottom: the status line
            // on the edge, the caption just above it.
            float bottom = Screen.height * 0.015f;
            string status = StatusLine();
            if (status.Length > 0) bottom += Pill(status, Mathf.Max(12, Screen.height / 72), bottom) + Screen.height * 0.008f;
            if (!string.IsNullOrEmpty(caption) && Time.unscaledTime <= captionUntil)
                Pill(caption, Mathf.Max(14, Screen.height / 55), bottom);
        }

        private void SpeedButtons()
        {
            if (MainSim.Inst == null) return;
            int fs = Mathf.Max(12, Screen.height / 60);
            if (speedStyle == null) speedStyle = new GUIStyle(GUI.skin.button);
            speedStyle.fontSize = fs;
            speedStyle.fontStyle = FontStyle.Bold;
            float h = fs * 2.2f, gap = fs * 0.4f, margin = Screen.height * 0.015f;
            float[] widths = { fs * 4.5f, fs * 6f, fs * 4.5f };
            float total = widths[0] + widths[1] + widths[2] + gap * 2;
            speedPanelW = total + margin * 2;
            float x = Screen.width - total - margin, y = Screen.height - h - margin;
            double cur = MainSim.Inst.TimeFactor;
            bool play = manualSpeed && manualFactor <= 1.0;
            bool ff = manualSpeed && manualFactor > 1.0;
            // The fast-forward button shows the speed it runs at now, or 2x when off.
            string ffLabel = "\u25B6\u25B6 " + (ff ? manualFactor.ToString("0") : "2") + "x";
            // A click here comes from a person, so the audit log records it.
            string mode = null;
            if (Button(new Rect(x, y, widths[0], h), "\u25B6 1x", play)) mode = "play";
            x += widths[0] + gap;
            if (Button(new Rect(x, y, widths[1], h), ffLabel, ff)) mode = "ff";
            x += widths[1] + gap;
            if (Button(new Rect(x, y, widths[2], h), "Auto", !manualSpeed)) mode = "auto";
            if (mode != null)
            {
                Speed(mode);
                Audit("speed_button", Kv("mode", mode), Kv("time_factor", (float)MainSim.Inst.TimeFactor));
            }
        }

        // The buttons' actions; the `speed` command runs the same code.
        private string Speed(string mode)
        {
            var ms = MainSim.Inst;
            if (mode == "play")
            {
                manualSpeed = true;
                manualFactor = 1.0;
            }
            else if (mode == "ff")
            {
                bool ff = manualSpeed && manualFactor > 1.0;
                manualFactor = ff ? (manualFactor >= 128.0 ? 2.0 : manualFactor * 2.0) : 2.0;
                manualSpeed = true;
            }
            else if (mode == "auto")
            {
                // The client's ramp takes over at its next check-in; until then keep the speed,
                // dropping back to 1x when no code runs.
                manualSpeed = false;
                ffFrame = Time.frameCount;
                ffUntilIdle = ms != null && ms.TimeFactor > 1.0;
            }
            else if (mode != "status") return Err("speed takes play, ff, auto or status");
            if (manualSpeed && ms != null) ms.TimeFactor = manualFactor;
            return Ok(Kv("manual", manualSpeed), Kv("time_factor", ms != null ? (float)ms.TimeFactor : 1f));
        }

        private bool Button(Rect r, string label, bool active)
        {
            var old = GUI.backgroundColor;
            GUI.backgroundColor = active ? new Color(0.35f, 0.75f, 1f) : new Color(0.25f, 0.27f, 0.32f);
            bool hit = GUI.Button(r, label, speedStyle);
            GUI.backgroundColor = old;
            return hit;
        }

        // Draws text in a dark box, centred horizontally, `bottom` pixels above the screen's
        // bottom edge. Returns the box height so callers can stack boxes.
        private float Pill(string text, int fontSize, float bottom)
        {
            captionStyle.fontSize = fontSize;
            int pad = fontSize / 2;
            captionStyle.padding = new RectOffset(pad * 2, pad * 2, pad, pad);
            // Measure the bare text, then add the padding ourselves: CalcHeight on the padded
            // style under-counts wrapped lines, which clipped the first line.
            captionStyle.clipping = TextClipping.Overflow;
            var bare = new GUIStyle(captionStyle) { padding = new RectOffset(0, 0, 0, 0) };
            bare.normal.background = null;
            // Centre over the farm, right of the code column, so the code stays readable.
            float left = columnRight > 0f && columnRight < Screen.width * 0.7f ? columnRight : 0f;
            float area = Screen.width - left - speedPanelW;
            float maxText = area * (left > 0f ? 0.94f : 0.6f) - pad * 4;
            var content = new GUIContent(text);
            float textW = Mathf.Min(maxText, bare.CalcSize(content).x + 2f);
            float textH = bare.CalcHeight(content, textW);
            float w = textW + pad * 4, h = textH + pad * 2;
            GUI.Label(new Rect(left + (area - w) / 2f, Screen.height - h - bottom, w, h), content, captionStyle);
            return h;
        }

        private static string Items(ItemBlock block)
        {
            var parts = new List<string>();
            if (block != null)
            {
                foreach (int id in block.ItemIds())
                {
                    double n = block.GetNumber(id);
                    if (n > 0) parts.Add(n.ToString("#,0") + " " + StringIds.GetItemName(id).Replace("_", " "));
                }
            }
            return parts.Count == 0 ? "nothing" : string.Join(", ", parts.ToArray());
        }

        private void LateUpdate()
        {
            if (runPending && Time.frameCount > runStartFrame && MainSim.Inst != null && !MainSim.Inst.IsExecuting())
            {
                runsFinished = runsStarted;
                if (!string.IsNullOrEmpty(runName) && wdStopped == "") Say("Finished running " + runName, 6f);
                Wait("");
                runPending = false;
                AuditRunEnd();
            }
            TrackRunSpeed();
            Watchdog();
            SweepStaged();
            // An auto fast-forward ends with the run, even if the client has gone away.
            if (manualSpeed && MainSim.Inst != null)
            {
                MainSim.Inst.TimeFactor = manualFactor;
                ffUntilIdle = false;
            }
            if (ffUntilIdle && Time.frameCount > ffFrame && MainSim.Inst != null && !MainSim.Inst.IsExecuting())
            {
                MainSim.Inst.TimeFactor = 1.0;
                ffUntilIdle = false;
            }
            StepCentering();
            StepFollow();
            StepTidy();
        }

        // Progress rule. With goal items set, only a rise in one of them counts. Without any,
        // a rise in any item counts except water and fertilizer, which refill on their own.
        // Only a rise counts: the drone burns power even while it idles.
        // Returns true when a counted item rose since the last call.
        private readonly double[] wdCounts = new double[256];
        private string wdRose;  // the last item that rose, for the check-in log

        private bool GoalMode => wdItems.Count > 0 || wdGoal >= 0;

        private bool Counts(int id, string name)
        {
            if (GoalMode) return wdItems.Contains(id) || id == wdGoal;
            return name != "water" && name != "fertilizer";
        }

        private bool Progressed(bool reset = false)
        {
            var ms = MainSim.Inst;
            bool rose = false;
            double total = 0;
            for (int id = 0; id < wdCounts.Length; id++)
            {
                double n;
                string name;
                try { name = StringIds.GetItemName(id); n = ms.GetNumItem(id); } catch (Exception) { break; }
                if (string.IsNullOrEmpty(name)) continue;
                if (!Counts(id, name)) continue;
                if (n > wdCounts[id] && !reset) { rose = true; wdRose = name; }
                wdCounts[id] = n;
                total += n;
            }
            // Progress for the check-in: the status goal's count, else the sum of the counted items.
            wdProgress = goalItem >= 0 ? ms.GetNumItem(goalItem) : total;
            return rose;
        }

        private string ItemNames(HashSet<int> ids, int extra)
        {
            var names = ids.Select(i => StringIds.GetItemName(i)).ToList();
            if (extra >= 0 && !ids.Contains(extra)) names.Add(StringIds.GetItemName(extra));
            return string.Join(",", names.ToArray());
        }

        private void Watchdog()
        {
            if (!runPending || MainSim.Inst == null || !MainSim.Inst.IsExecuting()) return;
            float now = Time.unscaledTime;
            if (now < wdNextSample) return;
            wdNextSample = now + 1f;
            if (Progressed()) wdProgressAt = now;
            double p = wdProgress;
            string why = null;
            if (wdLimit > 0 && now - wdStart > wdLimit) why = "ran past its " + Clock(wdLimit) + " limit";
            else if (wdStall > 0 && now - wdProgressAt > wdStall) why = "no progress for " + Clock(now - wdProgressAt);
            if (why != null)
            {
                wdStopped = why;
                CheckIn("stopped: " + why);
                MainSim.Inst.StopMainExecution();
                Say("Watchdog stopped " + runName + ": " + why, 60f);
                return;
            }
            if (now >= wdNextCheck)
            {
                wdNextCheck = now + CheckEvery;
                double left = goalItem >= 0 ? SecondsLeft(p) : -1;
                // A goal estimate past the limit means the run will be cut off: say so now.
                string note = left > 0 && wdLimit > 0 && now - wdStart + left > wdLimit ? "eta past limit" : "ok";
                CheckIn(note);
            }
        }

        private void CheckIn(string note)
        {
            var lb = UnityEngine.Object.FindObjectOfType<LeaderboardManager>();
            string line = "{" + string.Join(",", new[] {
                Kv("at", DateTime.Now.ToString("s")), Kv("run", runName),
                Kv("real_s", Mathf.Round(Time.unscaledTime - wdStart)),
                Kv("quiet_s", Mathf.Round(Time.unscaledTime - wdProgressAt)),
                Kv("limit_s", wdLimit), Kv("stall_s", wdStall),
                Kv("goal", goalItem >= 0 ? StringIds.GetItemName(goalItem) : null),
                Kv("counts", GoalMode ? ItemNames(wdItems, wdGoal) : "any item"),
                Kv("progress", wdProgress), Kv("target", goalTarget), Kv("last_rise", wdRose),
                Kv("board_clock", lb != null && lb.IsRunning ? TextOf(lb, "timeText") : null),
                Kv("speed", (float)MainSim.Inst.TimeFactor), Kv("note", note) }) + "}";
            Logger.LogInfo("watchdog " + line);
            try { File.AppendAllText(Path.Combine(Application.persistentDataPath, "claude_watchdog.log"), line + "\n"); }
            catch (Exception e) { Logger.LogWarning("watchdog log: " + e.Message); }
        }

        private string WatchdogCmd(string[] a)
        {
            for (int i = 1; i + 1 < a.Length; i += 2)
            {
                if (a[i] == "limit") { nextLimit = ParseF(a[i + 1]); if (runPending) wdLimit = nextLimit; }
                else if (a[i] == "stall") { nextStall = ParseF(a[i + 1]); if (runPending) wdStall = nextStall; }
                else if (a[i] == "items")
                {
                    var ids = new HashSet<int>();
                    foreach (var raw in a[i + 1].Split(','))
                    {
                        string name = raw.Trim().Replace("Items.", "").ToLowerInvariant();
                        if (name.Length == 0 || name == "none") continue;
                        int id = StringIds.GetItemId(name);
                        if (id < 0) return Err("no item named " + raw.Trim());
                        ids.Add(id);
                    }
                    nextItems.Clear();
                    nextItems.UnionWith(ids);
                    if (runPending) { wdItems.Clear(); wdItems.UnionWith(ids); }
                }
                else return Err("watchdog takes: limit SECONDS, stall SECONDS (0 = off), items a,b (none = clear)");
            }
            float now = Time.unscaledTime;
            // While a run goes, report its limits; otherwise the next run's.
            float limit = runPending ? wdLimit : nextLimit, stall = runPending ? wdStall : nextStall;
            string counts = runPending ? (GoalMode ? ItemNames(wdItems, wdGoal) : "any item")
                                       : (nextItems.Count > 0 || nextGoal >= 0 ? ItemNames(nextItems, nextGoal) : "any item");
            return Ok(Kv("limit", limit), Kv("stall", stall), Kv("counts", counts), Kv("running", runPending),
                      Kv("real_s", runPending ? now - wdStart : 0), Kv("quiet_s", runPending ? now - wdProgressAt : 0),
                      Kv("last_rise", wdRose), Kv("stopped", wdStopped));
        }

        // ---------------------------------------------------------------- audit log
        // Benchmark runs need a record a reviewer can trust. We append one JSON line per
        // client command to claude_audit.jsonl in the data folder, plus run, unlock and
        // intervention events. Code bodies appear as their SHA-256 and length; the code
        // itself goes to claude_runs/<sha256>.py, once per distinct body.
        // The read-only polls below come several times a second and change nothing, so
        // we leave them out (a run_and_wait polls status at 10 Hz).
        private static readonly HashSet<string> Polls = new HashSet<string> {
            "status", "error", "output", "windows", "lb", "farm_rect", "ping" };
        private static readonly object AuditLock = new object();
        private static string dataDir;

        private static void Audit(string evt, params string[] fields)
        {
            var sb = new StringBuilder("{");
            sb.Append(Kv("t", DateTime.UtcNow.ToString("o"))).Append(',').Append(Kv("event", evt));
            foreach (var f in fields)
                if (!string.IsNullOrEmpty(f)) sb.Append(',').Append(f);
            sb.Append('}');
            try
            {
                lock (AuditLock) File.AppendAllText(Path.Combine(dataDir, "claude_audit.jsonl"), sb + "\n");
            }
            catch (Exception) { }  // a full disk must not break the bridge
        }

        private static string Sha256(string text)
        {
            using (var h = System.Security.Cryptography.SHA256.Create())
            {
                var sb = new StringBuilder();
                foreach (byte b in h.ComputeHash(Encoding.UTF8.GetBytes(text))) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }

        // Saves a code body under claude_runs/<sha256>.py and returns the hash.
        private static string Archive(string code)
        {
            string sha = Sha256(code ?? "");
            try
            {
                string dir = Path.Combine(dataDir, "claude_runs");
                Directory.CreateDirectory(dir);
                string path = Path.Combine(dir, sha + ".py");
                if (!File.Exists(path)) File.WriteAllText(path, code ?? "", new UTF8Encoding(false));
            }
            catch (Exception) { }
            return sha;
        }

        private static string CodeRef(string code)
        {
            return "{" + Kv("sha256", Sha256(code)) + "," + Kv("len", code.Length) + "}";
        }

        // Runs on the main thread when a client request finishes.
        private static void AuditCommand(Job job)
        {
            string[] a = job.Args;
            string cmd = a.Length > 0 ? a[0] : "";
            if (Polls.Contains(cmd) || cmd == "intervene") return;
            if (cmd == "speed" && a.Length > 1 && a[1] == "status") return;
            if (cmd == "watchdog" && a.Length == 1) return;
            var args = new StringBuilder("[");
            for (int i = 1; i < a.Length; i++)
            {
                if (i > 1) args.Append(',');
                string v = a[i];
                bool code = (cmd == "set" || cmd == "run") && i == 2;
                bool text = (cmd == "say" || cmd == "wait") && i == 1;
                try
                {
                    if (code) { args.Append(CodeRef(Decode(v))); continue; }
                    if (text) v = Decode(v);
                }
                catch (Exception)
                {
                    // Never log a code body, not even one we can't decode.
                    if (code) v = "(undecodable)";
                }
                args.Append(Quote(v));
            }
            args.Append(']');
            bool ok = job.Result != null && job.Result.StartsWith("{\"ok\":true");
            string error = null;
            if (!ok && job.Result != null)
            {
                int i = job.Result.IndexOf("\"error\":", StringComparison.Ordinal);
                error = i >= 0 ? job.Result.Substring(i + 8).TrimEnd('}') : job.Result;
            }
            Audit("command", Kv("cmd", cmd), "\"args\":" + args, Kv("ok", ok), error != null ? "\"error\":" + error : null);
        }

        // The code a window holds now (for a run of code already in the window).
        private static string CodeOf(CodeWindow cw)
        {
            object input = cw.CodeInput;
            return input?.GetType().GetProperty("text")?.GetValue(input) as string;
        }

        // Real seconds spent at each clock factor during the current run, for the run_end line.
        private readonly SortedDictionary<int, float> runSpeeds = new SortedDictionary<int, float>();
        private double runFarmSeconds;
        private string runSha;
        private int runLen;

        private void TrackRunSpeed()
        {
            if (!runPending || MainSim.Inst == null) return;
            double f = MainSim.Inst.TimeFactor;
            int key = (int)Math.Round(f);
            runSpeeds.TryGetValue(key, out float t);
            runSpeeds[key] = t + Time.unscaledDeltaTime;
            runFarmSeconds += Time.unscaledDeltaTime * f;
        }

        private void AuditRunEnd()
        {
            var speeds = new StringBuilder("{");
            foreach (var pair in runSpeeds)
            {
                if (speeds.Length > 1) speeds.Append(',');
                speeds.Append(Kv(pair.Key.ToString(), (float)Math.Round(pair.Value, 2)));
            }
            speeds.Append('}');
            string err = null;
            if (!string.IsNullOrEmpty(runName) && workspace != null && workspace.codeWindows.TryGetValue(runName, out var cw) && cw != null)
                err = ErrorOf(cw);
            Audit("run_end", Kv("run_id", runsStarted), Kv("window", runName), Kv("sha256", runSha), Kv("len", runLen),
                  Kv("real_s", (float)Math.Round(Time.unscaledTime - wdStart, 2)), Kv("farm_s", (float)Math.Round(runFarmSeconds, 2)),
                  "\"speed_s\":" + speeds, Kv("watchdog", wdStopped == "" ? null : wdStopped), Kv("error", err),
                  Kv("save", OptionHolder.GetString("activeSave", "Save0")));
        }

        // Staging windows close once nothing runs, after a grace period so `set` then `run` works.
        private readonly Dictionary<string, float> staged = new Dictionary<string, float>();
        private float nextSweep;

        private void SweepStaged()
        {
            if (runPending || MainSim.Inst == null || MainSim.Inst.IsExecuting() || Time.unscaledTime < nextSweep) return;
            nextSweep = Time.unscaledTime + 1f;
            // A restart forgets the staged list, so adopt any claude_ window we don't know yet.
            foreach (var name in Ws.codeWindows.Keys.ToList())
                if (name.StartsWith("claude_") && !staged.ContainsKey(name)) staged[name] = Time.unscaledTime;
            foreach (var name in staged.Keys.ToList())
            {
                if (Time.unscaledTime - staged[name] < 15f) continue;
                staged.Remove(name);
                if (Ws.codeWindows.TryGetValue(name, out var cw) && cw != null) cw.GetComponent<Window>().Close();
            }
        }

        private void Dispatch(Job job)
        {
            string[] a = job.Args;
            switch (a[0])
            {
                case "ping":
                    job.Finish(Ok(
                        Kv("plugin", Version),
                        Kv("unity", Application.unityVersion),
                        Kv("platform", Application.platform.ToString()),
                        Kv("data_dir", Application.persistentDataPath),
                        Kv("focused", Application.isFocused),
                        Kv("active_save", OptionHolder.GetString("activeSave", "Save0"))));
                    return;
                case "play":
                    // The title screen's Start button.
                    FindMenu().Play();
                    job.Finish(Ok(Kv("in_game", Ws.gameObject.activeInHierarchy)));
                    return;
                case "wait":
                    // wait [B64TEXT [timeout_s [ITEM TARGET]]]: no text clears the pill.
                    Wait(a.Length > 1 ? Decode(a[1]) : "", a.Length > 2 ? ParseF(a[2]) : 0f,
                         a.Length > 4 ? a[3] : null, a.Length > 4 ? ParseF(a[4]) : 0);
                    waitFromClient = waitText.Length > 0;
                    job.Finish(Ok(Kv("wait", waitText)));
                    return;
                case "say":
                    Need(a, 2);
                    Say(Decode(a[1]), a.Length > 2 ? ParseF(a[2]) : 0f);
                    captionFromClient = true;
                    job.Finish(Ok());
                    return;
                case "watcher":
                    Need(a, 2);
                    job.Finish(Watcher(a[1] == "1"));
                    return;
                case "save":
                    // The menu's Save button.
                    FindMenu().PressSaveButton();
                    job.Finish(Ok());
                    return;
                case "windows":
                    job.Finish(Windows());
                    return;
                case "status":
                    job.Finish(Status());
                    return;
                case "set":
                    Need(a, 3);
                    job.Finish(SetCode(a[1], Decode(a[2])));
                    return;
                case "run":
                    Need(a, 2);
                    job.Finish(Run(a[1], a.Length > 2 ? Decode(a[2]) : null));
                    return;
                case "stop":
                    MainSim.Inst.StopMainExecution();
                    job.Finish(Ok());
                    return;
                case "output":
                    job.Finish(Ok(Kv("text", MainSim.Inst.Logger.GetOutputString())));
                    return;
                case "error":
                    Need(a, 2);
                    job.Finish(Ok(Kv("error", ErrorOf(FindWindow(a[1])))));
                    return;
                case "screenshot":
                    Need(a, 2);
                    StartCoroutine(Screenshot(job, a[1]));
                    return;
                case "center_farm":
                    StartCentering(job,
                        a.Length > 1 ? ParseF(a[1]) : 0.5f,
                        a.Length > 2 ? ParseF(a[2]) : 0.5f);
                    return;
                case "hover":
                    // Screen point in screenshot pixels (origin top-left), like a mouse hover.
                    Need(a, 3);
                    job.Finish(Hover(ParseF(a[1]), ParseF(a[2])));
                    return;
                case "fast_forward":
                    Need(a, 2);
                    job.Finish(FastForward(ParseF(a[1]), a.Length > 2 && a[2] == "1"));
                    return;
                case "buy":
                    Need(a, 2);
                    job.Finish(Buy(a[1]));
                    return;
                case "saves":
                    job.Finish(Saves());
                    return;
                case "new_save":
                    // The save chooser's "new save" button, then its load: a fresh farm in a new slot.
                    job.Finish(SwitchSave(a.Length > 1 && a[1] != "" ? a[1] : null, create: true));
                    return;
                case "load_save":
                    Need(a, 2);
                    job.Finish(SwitchSave(a[1], create: false));
                    return;
                case "close":
                    // Closing a code window deletes its file; the client only closes its own windows.
                    Need(a, 2);
                    FindWindow(a[1]).GetComponent<Window>().Close();
                    job.Finish(Ok());
                    return;
                case "close_docs":
                    job.Finish(CloseDocs());
                    return;
                case "lb":
                    job.Finish(LeaderboardState());
                    return;
                case "lb_ok":
                    job.Finish(LeaderboardOk());
                    return;
                case "arrange":
                    job.Finish(Arrange(a.Length > 1 ? ParseF(a[1]) : 0.36f, a.Length > 2 ? a[2] : ""));
                    return;
                case "check":
                    job.Finish(CheckAll());
                    return;
                case "farm_rect":
                    job.Finish(FarmRect());
                    return;
                case "follow":
                    Need(a, 2);
                    follow = a[1] == "1";
                    job.Finish(Ok(Kv("follow", follow)));
                    return;
                case "zoom":
                    Need(a, 2);
                    job.Finish(Zoom(ParseF(a[1])));
                    return;
                case "move":
                    Need(a, 4);
                    job.Finish(MoveWindow(a[1], ParseF(a[2]), ParseF(a[3])));
                    return;
                case "speed":
                    Need(a, 2);
                    job.Finish(Speed(a[1]));
                    return;
                case "tidy_guard":
                    Need(a, 2);
                    tidyGuard = a[1] == "1";
                    if (a.Length > 2) tidyKeep = a[2];
                    nextTidy = 0f;
                    job.Finish(Ok(Kv("tidy_guard", tidyGuard), Kv("keep", tidyKeep)));
                    return;
                case "watchdog":
                    job.Finish(WatchdogCmd(a));
                    return;
                case "inventory":
                    job.Finish(Inventory());
                    return;
                case "intervene":
                    // A person stepped in (typed code, bought a node, gave a hint): mark it in the audit log.
                    Need(a, 2);
                    {
                        string note = Decode(a[1]);
                        Audit("intervention", Kv("note", note), Kv("run_id", runsStarted), Kv("running", runPending));
                        Say("Human intervention: " + note, 8f);
                        job.Finish(Ok(Kv("note", note)));
                    }
                    return;
                case "minimize":
                    Need(a, 3);
                    FindWindow(a[1]).GetComponent<Window>().SetMinmized(a[2] == "1");
                    job.Finish(Ok());
                    return;
                default:
                    job.Finish(Err("unknown command: " + a[0]));
                    return;
            }
        }

        // ---------------------------------------------------------------- commands

        private Workspace Ws
        {
            get
            {
                if (workspace == null) workspace = MainSim.Inst?.workspace;
                if (workspace == null) throw new InvalidOperationException("no farm workspace (is a save loaded?)");
                return workspace;
            }
        }

        private static Menu FindMenu()
        {
#pragma warning disable CS0618 // FindObjectOfType is deprecated in Unity 6 but still works.
            var menu = FindObjectOfType<Menu>(true);
#pragma warning restore CS0618
            if (menu == null) throw new InvalidOperationException("no game menu found");
            return menu;
        }

        // Leaderboard runs end on a result screen that blocks the workspace until the
        // player presses OK. We read what the player sees there and press OK through
        // LeaderboardManager.OkPressed, the button's own handler.
        private static string TextOf(object owner, string field)
        {
            var f = owner.GetType().GetField(field, BindingFlags.NonPublic | BindingFlags.Instance);
            var t = f?.GetValue(owner);
            if (t is GameObject go)  // e.g. runCancelled: read the text component on it
                t = go.GetComponents<Component>().FirstOrDefault(c => c.GetType().GetProperty("text") != null);
            return t?.GetType().GetProperty("text")?.GetValue(t) as string;
        }

        private string LeaderboardState()
        {
            var m = UnityEngine.Object.FindObjectOfType<LeaderboardManager>();
            if (m == null) return Err("no leaderboard manager");
            return Ok(Kv("running", m.IsRunning), Kv("screen", m.IsLeaderBoardScreenOpen),
                      Kv("title", TextOf(m, "leaderboardTitle")), Kv("time", TextOf(m, "finishTimeText")),
                      Kv("result", TextOf(m, "runCancelled")), Kv("clock", TextOf(m, "timeText")));
        }

        private string LeaderboardOk()
        {
            var m = UnityEngine.Object.FindObjectOfType<LeaderboardManager>();
            if (m == null || !m.IsLeaderBoardScreenOpen) return Err("no leaderboard result screen open");
            m.OkPressed();
            return Ok();
        }

        private CodeWindow FindWindow(string name)
        {
            if (Ws.codeWindows.TryGetValue(name, out var cw) && cw != null) return cw;
            throw new InvalidOperationException("no code window named " + name);
        }

        private static string ErrorOf(CodeWindow cw)
        {
            return ErrorStringField?.GetValue(cw) as string;
        }

        private string Windows()
        {
            var sb = new StringBuilder("[");
            bool first = true;
            foreach (var pair in Ws.codeWindows)
            {
                if (pair.Value == null) continue;
                if (!first) sb.Append(',');
                first = false;
                var rect = pair.Value.GetComponent<RectTransform>();
                sb.Append('{')
                    .Append(Kv("name", pair.Key)).Append(',')
                    .Append(Kv("executing", pair.Value.isExecuting)).Append(',')
                    .Append(Kv("minimized", pair.Value.IsMinimized())).Append(',')
                    .Append(Kv("x", rect.anchoredPosition.x)).Append(',')
                    .Append(Kv("y", rect.anchoredPosition.y))
                    .Append('}');
            }
            sb.Append(']');
            return Ok("\"windows\":" + sb);
        }

        // The icon shrinks for 0.3 s after Dismiss, so ignore it until then.
        private bool WarningShown()
        {
            var icon = MainSim.Inst.warningIcon;
            return icon != null && Time.unscaledTime - warningClearedAt > 0.35f && icon.transform.localScale.x > 0.5f;
        }

        private string Status()
        {
            string running = null;
            foreach (var pair in Ws.codeWindows)
            {
                if (pair.Value != null && pair.Value.isExecuting) running = pair.Key;
            }
            return Ok(
                Kv("in_game", Ws.gameObject.activeInHierarchy),
                Kv("executing", MainSim.Inst.IsExecuting()),
                Kv("window", running),
                Kv("runs_started", runsStarted),
                Kv("runs_finished", runsFinished),
                Kv("warning", WarningShown()),
                Kv("goal_have", goalItem >= 0 ? MainSim.Inst.GetNumItem(goalItem) : 0),
                Kv("goal_reached", goalItem >= 0 && MainSim.Inst.GetNumItem(goalItem) >= goalTarget),
                Kv("watchdog_stopped", wdStopped),
                Kv("focused", Application.isFocused));
        }

        // Every item count, as the top bar shows them. During simulate() or a leaderboard run
        // these are the sandbox's counts, because the game swaps its simulation.
        private string Inventory()
        {
            var block = MainSim.Inst.GetInventory();
            var sb = new StringBuilder("{");
            if (block != null)
            {
                foreach (int id in block.ItemIds())
                {
                    if (sb.Length > 1) sb.Append(',');
                    sb.Append(Kv(StringIds.GetItemName(id), block.GetNumber(id)));
                }
            }
            sb.Append('}');
            return Ok(Kv("executing", MainSim.Inst.IsExecuting()), "\"items\":" + sb);
        }

        // Puts code into a window, opening the window if it doesn't exist yet.
        private static bool WatcherOn => WatcherField?.GetValue(null) != null;

        private string Watcher(bool on)
        {
            if (on && !WatcherOn) Saver.StartFileWatcher();
            if (!on) StopWatcher();
            return Ok(Kv("watcher", WatcherOn));
        }

        private static void StopWatcher()
        {
            if (WatcherOn) Saver.StopFileWatcher();
            // Drop file events already queued, so none lands after we inject.
            object gate = PendingLockField?.GetValue(null);
            if (PendingField?.GetValue(null) is System.Collections.IDictionary pending && gate != null)
            {
                lock (gate) pending.Clear();
            }
        }

        private string SetCode(string name, string code)
        {
            StopWatcher();
            Archive(code);
            if (Ws.codeWindows.TryGetValue(name, out var cw) && cw != null)
            {
                cw.ApplyExternalCode(code);
            }
            else
            {
                // Only main shows on screen. Any other window the bridge opens is a staging file
                // for simulate(), leaderboard_run() or import: it must carry the claude_ prefix
                // (so we never touch the player's files), starts minimized, and closes itself
                // after the next run (2026-11-03: stray windows kept piling up beside main).
                if (name != "main" && !name.StartsWith("claude_"))
                    return Err("only main and claude_ staging windows: " + name);
                // IsValidFileName also rejects names already open, so check only for new windows.
                if (!Ws.IsValidFileName(name)) return Err("invalid window name: " + name);
                Ws.OpenCodeWindow(name, code, Vector2.zero);
                cw = FindWindow(name);
            }
            if (name != "main")
            {
                cw.GetComponent<Window>().SetMinmized(true);
                staged[name] = Time.unscaledTime;
            }
            cw.Parse();
            return Ok(Kv("error", ErrorOf(cw)));
        }

        private string Run(string name, string code)
        {
            if (name != "main") return Err("runs go through main; stage other files with set, then run code in main that uses them");
            if (code != null)
            {
                string set = SetCode(name, code);
                if (set.StartsWith("{\"ok\":false")) return set;
            }
            var cw = FindWindow(name);
            if (cw.Parse() == null) return Err("parse error: " + ErrorOf(cw));
            if (cw.isExecuting && MainSim.Inst.IsExecuting()) return Err("already running: " + name);
            // A run that ended this frame hasn't been booked yet; book it before the new one starts.
            if (runPending)
            {
                runsFinished = runsStarted;
                runPending = false;
                AuditRunEnd();
            }
            string body = code ?? CodeOf(cw) ?? "";
            runSha = Archive(body);
            runLen = body.Length;
            // The same handler the Execute button calls.
            cw.PressExecuteOrStop();
            runName = name;
            if (!captionFromClient) Say("Running " + name + "...");
            captionFromClient = false;
            if (!waitFromClient) Wait(name + " to finish");
            waitFromClient = false;
            runsStarted++;
            runStartFrame = Time.frameCount;
            // Latch the watchdog settings for this run; the next run starts from the defaults.
            wdLimit = nextLimit; wdStall = nextStall;
            wdItems.Clear(); wdItems.UnionWith(nextItems); wdGoal = nextGoal;
            nextLimit = DefaultLimit; nextStall = DefaultStall; nextItems.Clear(); nextGoal = -1;
            wdStart = wdProgressAt = Time.unscaledTime;
            wdNextCheck = wdStart + CheckEvery;
            wdProgress = 0;
            Array.Clear(wdCounts, 0, wdCounts.Length);
            wdRose = null;
            Progressed(reset: true);
            wdStopped = "";
            runSpeeds.Clear();
            runFarmSeconds = 0;
            CheckIn("started");
            Audit("run_start", Kv("run_id", runsStarted), Kv("window", name), Kv("sha256", runSha), Kv("len", runLen),
                  Kv("limit_s", wdLimit), Kv("stall_s", wdStall), Kv("counts", GoalMode ? ItemNames(wdItems, wdGoal) : "any item"),
                  Kv("save", OptionHolder.GetString("activeSave", "Save0")));
            // The logger prints each warning once per code location, so the output
            // can miss a repeat. The warning icon pops up every time: hide it now,
            // and treat it popping up again as a warning from this run.
            MainSim.Inst.warningIcon.Dismiss();
            warningClearedAt = Time.unscaledTime;
            runPending = true;
            return Ok(Kv("started", name), Kv("run_id", runsStarted));
        }

        private IEnumerator Screenshot(Job job, string path)
        {
            yield return new WaitForEndOfFrame();
            Texture2D tex = null;
            try
            {
                tex = ScreenCapture.CaptureScreenshotAsTexture();
                string dir = Path.GetDirectoryName(Path.GetFullPath(path));
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                File.WriteAllBytes(path, tex.EncodeToPNG());
                job.Finish(Ok(Kv("path", Path.GetFullPath(path)), Kv("width", tex.width), Kv("height", tex.height)));
            }
            catch (Exception e)
            {
                job.Finish(Err("screenshot failed: " + e.Message));
            }
            finally
            {
                if (tex != null) Destroy(tex);
            }
        }

        // Mirrors Workspace.Zoom(), but pivots on the screen centre instead of the mouse.
        private string Zoom(float value)
        {
            var ws = Ws;
            var cam = ws.cameraController;
            var parent = ws.workspaceContainer.parent;
            Vector3 pivot = new Vector3(Screen.width * 0.5f, Screen.height * 0.5f, 0f);
            Vector3 before = parent.InverseTransformPoint(pivot);
            float min = (float)(MinZoomField?.GetValue(ws) ?? 0.05f);
            float max = (float)(MaxZoomField?.GetValue(ws) ?? 4f);
            cam.zoom = Mathf.Clamp(value, min, max);
            ws.zoomContainer.localScale = Vector3.one * cam.zoom;
            ws.workspaceContainer.GetComponent<ContainerScaler>()?.UpdateMarginSize();
            Vector3 after = parent.InverseTransformPoint(pivot);
            ws.workspaceContainer.anchoredPosition -= (Vector2)(before - after);
            return Ok(Kv("zoom", cam.zoom), Kv("min", min), Kv("max", max));
        }

        // Stacks windows top-down in a left column `width` (a screen fraction) wide, so the
        // farm has the rest of the screen. Code windows come first, sorted by name; the rest
        // (doc pages and the like) get minimized. Windows that don't fit open are minimized.
        private string Arrange(float width, string keep)
        {
            var ws = Ws;
            var canvas = ws.editorCanvas;
            Camera cam = canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera;
            var all = new List<Window>();
            foreach (var w in ws.workspaceContainer.GetComponentsInChildren<Window>())
            {
                if (w.gameObject.activeInHierarchy && w.movable) all.Add(w);
            }
            all.Sort((p, q) =>
            {
                if ((p.windowName == keep) != (q.windowName == keep)) return p.windowName == keep ? -1 : 1;
                bool pc = p.GetComponent<CodeWindow>() != null, qc = q.GetComponent<CodeWindow>() != null;
                if (pc != qc) return pc ? -1 : 1;
                return string.CompareOrdinal(p.windowName, q.windowName);
            });
            foreach (var w in all)
            {
                if (w.dockedParent != null) w.Undock();
            }
            float margin = 16f, gap = 10f;
            float top = Screen.height * 0.86f, bottom = margin;
            float y = top;
            var placed = new StringBuilder("[");
            float right = 0f;  // the column's real right edge: code windows widen to their longest line
            foreach (var w in all)
            {
                bool code = w.GetComponent<CodeWindow>() != null;
                if (!code && w.minimizable) w.SetMinmized(true);
                Rect r = ScreenRect(w.WindowRect, cam);
                float maxW = Screen.width * width - margin;
                if (w.resizable && r.width > maxW)
                {
                    // Narrow it to the column; sizeDelta is in canvas units, so scale by the ratio.
                    var sd = w.WindowRect.sizeDelta;
                    sd.x = Mathf.Max(w.minSize.x, sd.x * maxW / r.width);
                    w.WindowRect.sizeDelta = sd;
                    w.playerSetSize = sd;
                    w.UpdateSize();
                    r = ScreenRect(w.WindowRect, cam);
                }
                // Code windows grow to fit their longest line, so a resize may not stick.
                bool tooWide = r.width > maxW + 1f;
                bool kept = w.windowName == keep;
                if (kept && w.isMinimized) { w.SetMinmized(false); w.UpdateSize(); r = ScreenRect(w.WindowRect, cam); }
                if (kept && w.resizable)
                {
                    // The agent's window gets the column's height, up to 80% of the screen,
                    // minus a title bar's worth of room for each other window below it.
                    float others = (all.Count - 1) * (Screen.height * 0.045f + gap);
                    float wantH = Mathf.Min(Screen.height * 0.8f, top - bottom - others);
                    // It also fills the column's width, so long lines stay readable.
                    var sd = w.WindowRect.sizeDelta;
                    float perUnitY = r.height / Mathf.Max(1f, sd.y);
                    float perUnitX = r.width / Mathf.Max(1f, sd.x);
                    bool grow = false;
                    if (Mathf.Abs(wantH - r.height) > 2f && perUnitY > 0f) { sd.y = wantH / perUnitY; grow = true; }  // exact: a zoom-out must not leave it too tall
                    if (maxW > r.width + 1f && perUnitX > 0f) { sd.x = maxW / perUnitX; grow = true; }
                    if (grow)
                    {
                        w.WindowRect.sizeDelta = sd;
                        w.playerSetSize = sd;
                        w.UpdateSize();
                        r = ScreenRect(w.WindowRect, cam);
                    }
                }
                if (!kept && (tooWide || y - r.height < bottom) && w.minimizable && !w.isMinimized)
                {
                    w.SetMinmized(true);
                    w.UpdateSize();
                    r = ScreenRect(w.WindowRect, cam);
                }
                MoveTopLeftTo(w.WindowRect, new Vector2(margin, y), cam);
                ws.OnWindowRectChanged(w);
                if (placed.Length > 1) placed.Append(',');
                placed.Append('{').Append(Kv("name", w.windowName)).Append(',')
                    .Append(Kv("minimized", w.isMinimized)).Append('}');
                y -= r.height + gap;
                right = Mathf.Max(right, margin + r.width);
            }
            placed.Append(']');
            columnRight = right;
            return Ok(Kv("column_width", width), Kv("right", right / Screen.width), "\"windows\":" + placed);
        }

        private static Rect ScreenRect(RectTransform rt, Camera cam)
        {
            var corners = new Vector3[4];
            rt.GetWorldCorners(corners);
            Vector2 a = RectTransformUtility.WorldToScreenPoint(cam, corners[0]);
            Vector2 b = RectTransformUtility.WorldToScreenPoint(cam, corners[2]);
            return Rect.MinMaxRect(Mathf.Min(a.x, b.x), Mathf.Min(a.y, b.y), Mathf.Max(a.x, b.x), Mathf.Max(a.y, b.y));
        }

        // Moves a rect so its top-left corner lands on a screen point.
        private static void MoveTopLeftTo(RectTransform rt, Vector2 screenTopLeft, Camera cam)
        {
            var parent = (RectTransform)rt.parent;
            var corners = new Vector3[4];
            rt.GetWorldCorners(corners);
            Vector2 now = parent.InverseTransformPoint(corners[1]);
            RectTransformUtility.ScreenPointToLocalPointInRectangle(parent, screenTopLeft, cam, out Vector2 want);
            rt.anchoredPosition += want - now;
        }

        // The tile tooltip a player gets by resting the mouse on screen point (x, y).
        // We mirror MainSim.RaycastMouse: the same ray, the same visible-depth limit, so
        // only blocks a player could point at answer. Then the game's own GetHoveredTooltip().
        private string Hover(float x, float y)
        {
            var ms = MainSim.Inst;
            var sim = SimField?.GetValue(ms) as Simulation;
            var grid = sim?.farm?.grid;
            if (grid == null) return Err("no farm loaded");
            var point = new Vector3(x, Screen.height - y, 0f);
            Ray ray = ms.mainCamera.ScreenPointToRay(point);
            int maxDepth = grid.maxUncoveredDepth + 1;
            Vector3Int block;
            bool hit;
            lock (LockField?.GetValue(ms) ?? ms)
            {
                maxDepth = VisibleDepth(ms, grid, maxDepth);
                hit = grid.Raycast(ray, ms.SceneScaler.localToWorldMatrix, out block, out _, maxDepth);
            }
            if (!hit) return Ok(Kv("hit", false));
            Vector3Int saved = ms.hoveredBlock;
            ms.hoveredBlock = block;
            object info = GetHoveredTooltipMethod.Invoke(ms, null);
            ms.hoveredBlock = saved;
            Say("Looking at the field: hovering over (" + block.x + ", " + block.y + ")", 4f);
            return Ok(Kv("hit", true), Kv("x", block.x), Kv("y", block.y), Kv("z", -block.z), Kv("text", TooltipText(info)));
        }

        // Mining DLC: the farm renderer decides how deep the player can see; else one below uncovered.
        private static int VisibleDepth(MainSim ms, GridManager grid, int fallback)
        {
            if (!ms.InstalledDlcs.ToString().Contains("Min")) return fallback;
            try
            {
                var dcType = typeof(MainSim).Assembly.GetType("DesignConstants") ?? AppDomain.CurrentDomain.GetAssemblies()
                    .Select(asm => asm.GetType("DesignConstants")).FirstOrDefault(t => t != null);
                object dc = dcType.GetProperty("Inst", BindingFlags.Static | BindingFlags.Public).GetValue(null);
                object fr = dcType.GetField("farmRenderer").GetValue(dc);
                var m = fr.GetType().GetMethod("MaxVisibleDepth");
                return (int)m.Invoke(fr, new object[] { grid.WorldSize, grid.ComputeAverageDepth(), grid.maxUncoveredDepth, true });
            }
            catch (Exception)
            {
                return fallback;
            }
        }

        // TooltipInfo lives in another assembly; join its string fields and properties.
        private static string TooltipText(object info)
        {
            if (info == null) return null;
            var parts = new List<string>();
            foreach (var f in info.GetType().GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                if (f.FieldType == typeof(string) && f.GetValue(info) is string v && v.Length > 0) parts.Add(v);
            return string.Join("\n", parts);
        }

        // The sim's own fast-forward knob (simulate() and leaderboards use it). 1 = real time.
        // untilIdle: drop back to 1 when no code runs any more.
        private string FastForward(float factor, bool untilIdle)
        {
            var ms = MainSim.Inst;
            if (manualSpeed)
                return Ok(Kv("time_factor", (float)ms.TimeFactor), Kv("manual", true));
            factor = Mathf.Clamp(factor, 1f, 128f);
            ms.TimeFactor = factor;
            ffUntilIdle = untilIdle && factor > 1f;
            ffFrame = Time.frameCount;
            return Ok(Kv("time_factor", (float)ms.TimeFactor), Kv("until_idle", ffUntilIdle));
        }

        // Clicks a tech-tree node, exactly as the player would. A node the player can't press
        // (locked, too expensive) is refused, so prerequisites and costs still apply.
        private string Buy(string name)
        {
            name = name.Replace("Unlocks.", "").ToLowerInvariant();
            UnlockBox box = FindBox(name);
            if (box == null)
            {
                MainSim.Inst.researchMenu.Setup();
                box = FindBox(name);
            }
            if (box == null) return Err("no tech-tree node named " + name + " is visible yet");
            var button = box.GetComponentInChildren<Button>(true);
            if (button != null && !button.interactable) return Err("the " + name + " node can't be pressed yet (locked or too expensive)");
            int before = MainSim.Inst.NumUnlocked(box.unlockSO.unlockName);
            string cost = Items(MainSim.Inst.GetUnlockCost(box.unlockSO));
            box.ButtonClicked();
            int after = MainSim.Inst.NumUnlocked(box.unlockSO.unlockName);
            if (after <= before) return Err("the game refused to unlock " + name + " (not enough items?)");
            Audit("unlock", Kv("unlock", box.unlockSO.unlockName), Kv("level", after), Kv("cost", cost));
            Say("Upgraded " + box.unlockSO.unlockName.Replace("_", " ") + " to level " + after + " for " + cost, 8f);
            return Ok(Kv("unlock", box.unlockSO.unlockName), Kv("level", after), Kv("cost", cost));
        }

        private static UnlockBox FindBox(string name)
        {
            foreach (var b in Resources.FindObjectsOfTypeAll<UnlockBox>())
            {
                if (b.unlockSO != null && b.gameObject.scene.IsValid() && b.unlockSO.unlockName.ToLowerInvariant() == name) return b;
            }
            return null;
        }

        private string Saves()
        {
            var dir = Path.Combine(Application.persistentDataPath, "Saves");
            var sb = new StringBuilder("[");
            foreach (var d in Directory.GetDirectories(dir))
            {
                if (sb.Length > 1) sb.Append(',');
                sb.Append(Quote(Path.GetFileName(d)));
            }
            sb.Append(']');
            return Ok(Kv("active", OptionHolder.GetString("activeSave", "Save0")), "\"saves\":" + sb);
        }

        // Mirrors SaveChooser.CreateNewSave + LoadSave: save the current game, switch the
        // active slot, and reload the scene, which loads that slot. The plugin survives the reload.
        private string SwitchSave(string name, bool create)
        {
            if (MainSim.Inst != null && MainSim.Inst.dirty && !Saver.Save(MainSim.Inst))
                return Err("could not save the current game first");
            if (create)
            {
                name = name ?? SaveChooser.GenerateUnusedSaveName();
                if (File.Exists(Saver.GetPathOfSaveFile(name))) return Err("save already exists: " + name);
                Saver.CreateNewSaveGame(name);
            }
            else if (!Directory.Exists(Path.Combine(Application.persistentDataPath, "Saves", name)))
            {
                return Err("no such save: " + name);
            }
            Saver.StopFileWatcher();
            FindMenu().LoadSave(name);
            centerJob?.Finish(Err("the save changed"));
            centerJob = null;
            SceneManager.LoadScene(0);
            return Ok(Kv("save", name));
        }

        // Closes every window that isn't a code window (doc pages and the like).
        private string CloseDocs()
        {
            int n = 0;
            foreach (var w in Ws.workspaceContainer.GetComponentsInChildren<Window>())
            {
                if (w.GetComponent<CodeWindow>() != null || !w.gameObject.activeInHierarchy || !w.movable) continue;
                w.Close();
                n++;
            }
            return Ok(Kv("closed", n));
        }

        // Parses every code window and reports the ones that fail, without running anything.
        private string CheckAll()
        {
            var sb = new StringBuilder("[");
            int bad = 0;
            foreach (var pair in Ws.codeWindows)
            {
                if (pair.Value == null) continue;
                bool ok = pair.Value.Parse() != null;
                if (sb.Length > 1) sb.Append(',');
                sb.Append('{').Append(Kv("name", pair.Key)).Append(',').Append(Kv("ok", ok));
                if (!ok) { bad++; sb.Append(',').Append(Kv("error", ErrorOf(pair.Value))); }
                sb.Append('}');
            }
            sb.Append(']');
            return Ok(Kv("errors", bad), "\"windows\":" + sb);
        }

        private string MoveWindow(string name, float x, float y)
        {
            var rect = FindWindow(name).GetComponent<RectTransform>();
            rect.anchoredPosition = new Vector2(x, y);
            Ws.OnWindowRectChanged(rect.GetComponent<Window>());
            return Ok();
        }

        // Pans the workspace until the drawn farm's centre sits at (fx, fy) of the screen.
        // The 3D farm follows the camera, whose mapping from the 2D workspace we don't
        // model. Instead we step, watch how far the farm moved on screen, and learn
        // the gain per axis (a secant method). It converges in a few frames.
        private void StartCentering(Job job, float fx, float fy)
        {
            centerJob?.Finish(Err("superseded by a newer center_farm"));
            centerJob = job;
            centerTarget = new Vector2(Screen.width * fx, Screen.height * fy);
            centerFrames = 0;
            centerGain = Vector2.zero;
            centerLastStep = Vector2.zero;
        }

        private Vector2 centerGain;
        private Vector2 centerLastStep;
        private Vector2 centerLastError;

        private void StepCentering()
        {
            if (centerJob == null) return;
            var ws = Ws;
            if (!TryFarmScreenCentre(out Vector2 screen))
            {
                centerJob.Finish(Err("farm is not visible to the camera"));
                centerJob = null;
                return;
            }
            // The game tweens the camera to a window on errors and focus changes; that tween
            // would overwrite our pan, so we cancel it while we center.
            TransitionTargetField?.SetValue(ws, null);
            Vector2 error = centerTarget - screen;
            if (error.magnitude < 6f || centerFrames >= 40)
            {
                centerJob.Finish(error.magnitude < 6f
                    ? Ok(Kv("frames", centerFrames))
                    : Err("could not center the farm; pixel error " + error.magnitude));
                centerJob = null;
                return;
            }
            if (centerFrames == 0)
            {
                // First guess: one canvas unit moves the view by scaleFactor * zoom pixels.
                float g = 1f / (ws.editorCanvas.scaleFactor * Mathf.Max(0.01f, ws.cameraController.zoom));
                centerGain = new Vector2(g, g);
            }
            else
            {
                centerGain.x = LearnGain(centerGain.x, centerLastStep.x, centerLastError.x - error.x);
                centerGain.y = LearnGain(centerGain.y, centerLastStep.y, centerLastError.y - error.y);
            }
            Vector2 step = new Vector2(error.x * centerGain.x, error.y * centerGain.y) * 0.8f;
            PanTarget(ws).anchoredPosition += step;
            centerLastStep = step;
            centerLastError = error;
            centerFrames++;
        }

        // follow mode: when the farm drifts far from the centre and nothing else moves the
        // camera, start a new centering. Off by default, so the player's own panning wins.
        private bool follow;
        private float nextFollowCheck;

        private void StepFollow()
        {
            if (!follow || centerJob != null || Time.unscaledTime < nextFollowCheck) return;
            nextFollowCheck = Time.unscaledTime + 0.5f;
            var ws = Ws;
            if (ws == null || !ws.gameObject.activeInHierarchy) return;
            if (TransitionTargetField?.GetValue(ws) != null) return;
            if (Input.GetMouseButton(0) || Input.GetMouseButton(1) || Input.GetMouseButton(2)) return;
            if (!TryFarmScreenCentre(out Vector2 screen)) return;
            Vector2 off = screen - new Vector2(Screen.width * 0.5f, Screen.height * 0.5f);
            if (Mathf.Abs(off.x) < Screen.width * 0.15f && Mathf.Abs(off.y) < Screen.height * 0.15f) return;
            StartCentering(new Job { Args = new[] { "follow" } }, 0.5f, 0.5f);
        }

        // Tidy guard: code windows must never cover the farm. Once a second, if a window
        // overlaps the farm or the farm leaves the screen (a camera tween, a new window, a
        // resized game window), restack the windows in the left column, shrink the zoom if
        // the farm doesn't fit beside them, and center the farm in the free area. It holds
        // off while a mouse button is down, so the player can still drag things.
        private bool tidyGuard = true;
        private string tidyKeep = "main";  // the window the guard keeps open ("" = none, e.g. a farm-only video)
        private float nextTidy;
        private const float TidyColumn = 0.45f;  // the client's COLUMN

        private bool Covered(Rect farm)
        {
            var canvas = Ws.editorCanvas;
            Camera cam = canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera;
            foreach (var w in Ws.workspaceContainer.GetComponentsInChildren<Window>())
            {
                if (!w.gameObject.activeInHierarchy || !w.movable) continue;
                Rect r = ScreenRect(w.WindowRect, cam);
                if (r.xMax > farm.xMin + 4f && r.xMin < farm.xMax - 4f && r.yMax > farm.yMin + 4f && r.yMin < farm.yMax - 4f) return true;
            }
            return false;
        }

        private bool KeptVisible()
        {
            if (string.IsNullOrEmpty(tidyKeep) || !Ws.codeWindows.TryGetValue(tidyKeep, out var cw) || cw == null) return true;
            var canvas = Ws.editorCanvas;
            Camera cam = canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera;
            Rect r = ScreenRect(cw.GetComponent<Window>().WindowRect, cam);
            return r.xMin >= -2f && r.yMax <= Screen.height + 2f && r.yMax > Screen.height * 0.5f;
        }

        private static bool OnScreen(Rect farm)
        {
            return farm.xMin >= 0f && farm.xMax <= Screen.width && farm.yMin >= Screen.height * 0.08f && farm.yMax <= Screen.height * 0.92f;
        }

        // Zoom growth needs hysteresis: growing also widens the code windows, which can
        // force a shrink right back. Grow at most every 20 s, and never within 60 s of a
        // shrink (2026-11-04: shrink-only zoom ratcheted down to unreadable on a 4K TV).
        private float lastShrinkAt = -999f, lastGrowAt = -999f;
        private const float GrowWhenFitAbove = 1.6f, MaxTidyZoom = 0.6f;

        private float FitScale(Rect farm)
        {
            float left = Mathf.Max(columnRight, 0f) + 16f;
            return Mathf.Min((Screen.width - left) * 0.92f / farm.width, Screen.height * 0.72f / farm.height);
        }

        private bool TooSmall(Rect farm)
        {
            float now = Time.unscaledTime;
            return FitScale(farm) > GrowWhenFitAbove && Ws.cameraController.zoom < MaxTidyZoom - 0.01f
                   && now - lastShrinkAt > 60f && now - lastGrowAt > 20f;
        }

        private void StepTidy()
        {
            if (!tidyGuard || centerJob != null || Time.unscaledTime < nextTidy) return;
            nextTidy = Time.unscaledTime + 1f;
            var ws = Ws;
            if (ws == null || !ws.gameObject.activeInHierarchy) return;
            if (TransitionTargetField?.GetValue(ws) != null) return;
            if (Input.GetMouseButton(0) || Input.GetMouseButton(1) || Input.GetMouseButton(2)) return;
            if (!TryFarmScreenBounds(out Rect farm)) return;
            bool small = TooSmall(farm);
            if (!small && !Covered(farm) && OnScreen(farm) && KeptVisible()) return;
            Arrange(TidyColumn, tidyKeep);
            if (!TryFarmScreenBounds(out farm)) return;
            small = TooSmall(farm);
            if (!small && !Covered(farm) && OnScreen(farm)) return;
            float left = Mathf.Max(columnRight, 0f) + 16f;
            float scale = FitScale(farm);
            float z = ws.cameraController.zoom;
            if (scale < 0.97f && z > 0.081f)
            {
                Zoom(Mathf.Clamp(z * scale, 0.08f, MaxTidyZoom));
                lastShrinkAt = Time.unscaledTime;
            }
            else if (small)
            {
                Zoom(Mathf.Clamp(z * scale * 0.9f, 0.08f, MaxTidyZoom));
                lastGrowAt = Time.unscaledTime;
            }
            StartCentering(new Job { Args = new[] { "tidy" } }, (left + Screen.width) * 0.5f / Screen.width, 0.5f);
        }

        private string FarmRect()
        {
            if (!TryFarmScreenBounds(out Rect r)) return Err("farm is not visible to the camera");
            return Ok(Kv("x", r.xMin), Kv("y", r.yMin), Kv("w", r.width), Kv("h", r.height),
                Kv("tiles", MainSim.Inst.GetRenderPackage().groundData.sizexy.x), Kv("zoom", Ws.cameraController.zoom), Kv("screen_w", Screen.width), Kv("screen_h", Screen.height));
        }

        // The camera follows CameraController.contentRect; pan that when we can find it.
        private static RectTransform PanTarget(Workspace ws)
        {
            return ContentRectField?.GetValue(ws.cameraController) as RectTransform ?? ws.workspaceContainer;
        }

        // gain = step / (error removed per unit step); keeps the old gain if the step did nothing.
        private static float LearnGain(float gain, float step, float errorRemoved)
        {
            if (Mathf.Abs(step) < 1e-3f || Mathf.Abs(errorRemoved) < 0.5f) return gain;
            return step / errorRemoved;
        }

        // The screen-space centre of the farm's drawn bounds.
        private bool TryFarmScreenCentre(out Vector2 screen)
        {
            bool ok = TryFarmScreenBounds(out Rect r);
            screen = r.center;
            return ok;
        }

        // Projects the farm surface's four corners, the way FarmRenderer frames the farm.
        // It needs only the grid size, which the player sees anyway (get_world_size()).
        private bool TryFarmScreenBounds(out Rect rect)
        {
            rect = default;
            var camCtl = Ws?.cameraController;
            var cam = camCtl?.GetComponent<Camera>();
            var root = camCtl == null ? null : SceneScalerField?.GetValue(camCtl) as Transform;
            var rp = MainSim.Inst?.GetRenderPackage();
            var size = rp?.groundData?.sizexy ?? Vector2Int.zero;
            // The renderer draws a cell at local height -depth (FarmRenderer); a dug-out farm
            // sits far below z = 0, so frame the average surface, as the game's camera does.
            float zs = rp != null ? -(float)rp.averageDepth : 0f;
            if (cam == null || root == null || size.x <= 0 || size.y <= 0) return false;
            Matrix4x4 m = root.localToWorldMatrix;
            Vector2 min = new Vector2(float.MaxValue, float.MaxValue), max = -min;
            foreach (var c in new[]
            {
                new Vector3(0.5f, -0.5f, zs), new Vector3(0.5f - size.x, -0.5f, zs),
                new Vector3(0.5f, size.y - 0.5f, zs), new Vector3(0.5f - size.x, size.y - 0.5f, zs),
            })
            {
                Vector3 p = cam.WorldToScreenPoint(m.MultiplyPoint(c));
                if (p.z <= 0f) return false;
                min = Vector2.Min(min, p);
                max = Vector2.Max(max, p);
            }
            rect = Rect.MinMaxRect(min.x, min.y, max.x, max.y);
            return true;
        }

        // ---------------------------------------------------------------- helpers

        private static void Need(string[] a, int n)
        {
            if (a.Length < n) throw new ArgumentException($"{a[0]} needs {n - 1} argument(s)");
        }

        private static string Decode(string b64)
        {
            return Encoding.UTF8.GetString(Convert.FromBase64String(b64));
        }

        private static float ParseF(string s)
        {
            return float.Parse(s, System.Globalization.CultureInfo.InvariantCulture);
        }

        private static string Ok(params string[] fields)
        {
            var sb = new StringBuilder("{\"ok\":true");
            foreach (var f in fields)
            {
                if (string.IsNullOrEmpty(f)) continue;
                sb.Append(',').Append(f);
            }
            return sb.Append('}').ToString();
        }

        private static string Err(string message)
        {
            return "{\"ok\":false," + Kv("error", message) + "}";
        }

        private static string Kv(string key, object value)
        {
            string v;
            if (value == null) v = "null";
            else if (value is bool b) v = b ? "true" : "false";
            else if (value is int || value is long) v = value.ToString();
            else if (value is float f) v = float.IsNaN(f) || float.IsInfinity(f) ? "null" : f.ToString("R", System.Globalization.CultureInfo.InvariantCulture);
            else if (value is double d) v = double.IsNaN(d) || double.IsInfinity(d) ? "null" : d.ToString("R", System.Globalization.CultureInfo.InvariantCulture);
            else v = Quote(value.ToString());
            return Quote(key) + ":" + v;
        }

        private static string Quote(string s)
        {
            var sb = new StringBuilder("\"");
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }
    }
}
