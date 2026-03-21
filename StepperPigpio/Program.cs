using System;
using System.Collections.Generic;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Threading;

internal static class Program
{
    private const uint PI_OUTPUT = 1;
    private const uint PI_WAVE_MODE_ONE_SHOT = 0;
    private const uint PI_WAVE_MODE_REPEAT = 1;

    [StructLayout(LayoutKind.Sequential)]
    public struct gpioPulse_t
    {
        public uint gpioOn;
        public uint gpioOff;
        public uint usDelay;
    }

    private static class Pigpio
    {
        [DllImport("pigpio", EntryPoint = "gpioInitialise")]
        public static extern int gpioInitialise();

        [DllImport("pigpio", EntryPoint = "gpioTerminate")]
        public static extern void gpioTerminate();

        [DllImport("pigpio", EntryPoint = "gpioSetMode")]
        public static extern int gpioSetMode(uint gpio, uint mode);

        [DllImport("pigpio", EntryPoint = "gpioWrite")]
        public static extern int gpioWrite(uint gpio, uint level);

        [DllImport("pigpio", EntryPoint = "gpioWaveClear")]
        public static extern int gpioWaveClear();

        [DllImport("pigpio", EntryPoint = "gpioWaveAddGeneric")]
        public static extern int gpioWaveAddGeneric(uint numPulses, gpioPulse_t[] pulses);

        [DllImport("pigpio", EntryPoint = "gpioWaveCreate")]
        public static extern int gpioWaveCreate();

        [DllImport("pigpio", EntryPoint = "gpioWaveDelete")]
        public static extern int gpioWaveDelete(uint waveId);

        [DllImport("pigpio", EntryPoint = "gpioWaveTxSend")]
        public static extern int gpioWaveTxSend(uint waveId, uint waveMode);

        [DllImport("pigpio", EntryPoint = "gpioWaveTxStop")]
        public static extern int gpioWaveTxStop();
    }

    private sealed class Options
    {
        public int Pul { get; set; } = 13;
        public int Dir { get; set; } = 5;
        public int Ena { get; set; } = 8;

        public bool EnaActiveLow { get; set; } = false;
        public bool DirInvert { get; set; } = false;

        public double Freq { get; set; } = 800.0;
        public int Steps { get; set; } = 1600;
        public double Pause { get; set; } = 0.3;
        public int Loops { get; set; } = 0;
        public int Accel { get; set; } = 0;

        public string Move { get; set; } = "down"; // down | up
        public string Mode { get; set; } = "one";  // one | pingpong | continuous
    }

    public static int Main(string[] args)
    {
        var opt = ParseArgs(args);
        Console.WriteLine("测试");

        if (Pigpio.gpioInitialise() < 0)
        {
            Console.Error.WriteLine("pigpio 初始化失败。请确认已安装 pigpio，并以 root 运行，或确认系统环境允许访问 GPIO。");
            Console.Error.WriteLine("如果你平时使用的是 pigpiod，也建议先执行：sudo systemctl start pigpiod");
            return 1;
        }

        int pinPul = opt.Pul;
        int pinDir = opt.Dir;
        int? pinEna = opt.Ena < 0 ? null : opt.Ena;

        try
        {
            Check("gpioSetMode PUL", Pigpio.gpioSetMode((uint)pinPul, PI_OUTPUT));
            Check("gpioSetMode DIR", Pigpio.gpioSetMode((uint)pinDir, PI_OUTPUT));
            if (pinEna.HasValue)
                Check("gpioSetMode ENA", Pigpio.gpioSetMode((uint)pinEna.Value, PI_OUTPUT));

            // idle level
            Check("gpioWrite PUL idle", Pigpio.gpioWrite((uint)pinPul, 0));

            // enable motor
            SetEnable(pinEna, true, opt.EnaActiveLow);
            Thread.Sleep(100);

            bool oneWayForward = opt.Move.Equals("up", StringComparison.OrdinalIgnoreCase);

            void WriteDir(bool forward)
            {
                int level = forward ? 1 : 0;
                if (opt.DirInvert) level ^= 1;
                Check("gpioWrite DIR", Pigpio.gpioWrite((uint)pinDir, (uint)level));
                Thread.Sleep(1); // DIR setup time
            }

            void MoveWithOptionalRamp(int steps, double baseFreq)
            {
                if (opt.Accel <= 0 || steps <= 2 * opt.Accel)
                {
                    PulseSteps(pinPul, steps, baseFreq);
                    return;
                }

                int ramp = opt.Accel;
                double fStart = Math.Max(50.0, baseFreq * 0.2);

                // ramp-up
                for (int i = 0; i < ramp; i++)
                {
                    double f = fStart + (baseFreq - fStart) * (i + 1) / ramp;
                    PulseSteps(pinPul, 1, f);
                }

                // cruise
                PulseSteps(pinPul, steps - 2 * ramp, baseFreq);

                // ramp-down
                for (int i = 0; i < ramp; i++)
                {
                    double f = baseFreq - (baseFreq - fStart) * (i + 1) / ramp;
                    PulseSteps(pinPul, 1, f);
                }
            }

            Console.CancelKeyPress += (_, e) =>
            {
                e.Cancel = true;
                try
                {
                    Pigpio.gpioWaveTxStop();
                }
                catch { }
            };

            if (opt.Mode.Equals("one", StringComparison.OrdinalIgnoreCase))
            {
                WriteDir(oneWayForward);
                MoveWithOptionalRamp(opt.Steps, opt.Freq);
            }
            else if (opt.Mode.Equals("continuous", StringComparison.OrdinalIgnoreCase))
            {
                WriteDir(oneWayForward);
                while (true)
                {
                    PulseSteps(pinPul, 200, opt.Freq);
                }
            }
            else if (opt.Mode.Equals("pingpong", StringComparison.OrdinalIgnoreCase))
            {
                int loop = 0;
                while (true)
                {
                    WriteDir(true);
                    MoveWithOptionalRamp(opt.Steps, opt.Freq);
                    SleepSeconds(opt.Pause);

                    WriteDir(false);
                    MoveWithOptionalRamp(opt.Steps, opt.Freq);
                    SleepSeconds(opt.Pause);

                    loop++;
                    if (opt.Loops > 0 && loop >= opt.Loops)
                        break;
                }
            }
            else
            {
                Console.Error.WriteLine($"未知 mode: {opt.Mode}");
                return 2;
            }

            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("运行失败：");
            Console.Error.WriteLine(ex);
            return 3;
        }
        finally
        {
            try
            {
                SetEnable(pinEna, false, opt.EnaActiveLow);
                Pigpio.gpioWaveClear();
            }
            catch { }

            Pigpio.gpioTerminate();
        }
    }

    private static void PulseSteps(int pinPul, int steps, double freqHz, double duty = 0.5)
    {
        if (steps <= 0) return;
        if (freqHz <= 0) throw new ArgumentOutOfRangeException(nameof(freqHz), "freqHz must be > 0");

        int periodUs = (int)Math.Round(1_000_000.0 / freqHz);
        int highUs = Math.Max(1, (int)Math.Round(periodUs * duty));
        int lowUs = Math.Max(1, periodUs - highUs);

        var pulses = new[]
        {
            new gpioPulse_t
            {
                gpioOn = 1u << pinPul,
                gpioOff = 0,
                usDelay = (uint)highUs
            },
            new gpioPulse_t
            {
                gpioOn = 0,
                gpioOff = 1u << pinPul,
                usDelay = (uint)lowUs
            }
        };

        Check("gpioWaveClear", Pigpio.gpioWaveClear());
        int added = Pigpio.gpioWaveAddGeneric((uint)pulses.Length, pulses);
        if (added < 0) throw new InvalidOperationException($"gpioWaveAddGeneric failed: {added}");

        int wid = Pigpio.gpioWaveCreate();
        if (wid < 0) throw new InvalidOperationException($"gpioWaveCreate failed: {wid}");

        try
        {
            int rc = Pigpio.gpioWaveTxSend((uint)wid, PI_WAVE_MODE_REPEAT);
            if (rc < 0) throw new InvalidOperationException($"gpioWaveTxSend failed: {rc}");

            double seconds = steps * periodUs / 1_000_000.0 + 0.01;
            SleepSeconds(seconds);

            Check("gpioWaveTxStop", Pigpio.gpioWaveTxStop());
        }
        finally
        {
            Check("gpioWaveDelete", Pigpio.gpioWaveDelete((uint)wid));
        }
    }

    private static void SetEnable(int? pinEna, bool enable, bool enaActiveLow)
    {
        if (!pinEna.HasValue) return;

        int level;
        if (enaActiveLow)
            level = enable ? 0 : 1;
        else
            level = enable ? 1 : 0;

        Check("gpioWrite ENA", Pigpio.gpioWrite((uint)pinEna.Value, (uint)level));
    }

    private static void Check(string name, int rc)
    {
        if (rc < 0)
            throw new InvalidOperationException($"{name} failed: {rc}");
    }

    private static void SleepSeconds(double seconds)
    {
        if (seconds <= 0) return;
        Thread.Sleep(TimeSpan.FromSeconds(seconds));
    }

    private static Options ParseArgs(string[] args)
    {
        var opt = new Options();
        var map = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);

        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (!a.StartsWith("--", StringComparison.Ordinal))
                continue;

            string key = a[2..];

            if (key is "ena-active-low" or "dir-invert")
            {
                map[key] = "true";
                continue;
            }

            if (i + 1 >= args.Length)
                throw new ArgumentException($"参数 {a} 缺少值");

            map[key] = args[++i];
        }

        if (map.TryGetValue("pul", out var vPul)) opt.Pul = ParseInt(vPul!, "--pul");
        if (map.TryGetValue("dir", out var vDir)) opt.Dir = ParseInt(vDir!, "--dir");
        if (map.TryGetValue("ena", out var vEna)) opt.Ena = ParseInt(vEna!, "--ena");

        if (map.TryGetValue("ena-active-low", out _)) opt.EnaActiveLow = true;
        if (map.TryGetValue("dir-invert", out _)) opt.DirInvert = true;

        if (map.TryGetValue("freq", out var vFreq)) opt.Freq = ParseDouble(vFreq!, "--freq");
        if (map.TryGetValue("steps", out var vSteps)) opt.Steps = ParseInt(vSteps!, "--steps");
        if (map.TryGetValue("pause", out var vPause)) opt.Pause = ParseDouble(vPause!, "--pause");
        if (map.TryGetValue("loops", out var vLoops)) opt.Loops = ParseInt(vLoops!, "--loops");
        if (map.TryGetValue("accel", out var vAccel)) opt.Accel = ParseInt(vAccel!, "--accel");
        if (map.TryGetValue("move", out var vMove)) opt.Move = vMove!;
        if (map.TryGetValue("mode", out var vMode)) opt.Mode = vMode!;

        if (opt.Move is not ("up" or "down"))
            throw new ArgumentException("--move 只能是 up 或 down");

        if (opt.Mode is not ("one" or "pingpong" or "continuous"))
            throw new ArgumentException("--mode 只能是 one / pingpong / continuous");

        return opt;
    }

    private static int ParseInt(string s, string name)
    {
        if (!int.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out int v))
            throw new ArgumentException($"{name} 不是合法整数: {s}");
        return v;
    }

    private static double ParseDouble(string s, string name)
    {
        if (!double.TryParse(s, NumberStyles.Float | NumberStyles.AllowThousands, CultureInfo.InvariantCulture, out double v))
            throw new ArgumentException($"{name} 不是合法数字: {s}");
        return v;
    }
}
