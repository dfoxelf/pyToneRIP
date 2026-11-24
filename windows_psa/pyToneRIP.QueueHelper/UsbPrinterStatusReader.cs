using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using pyToneRIP.Models;
using pyToneRIP.Shared;

namespace pyToneRIP.QueueHelper
{
    internal static class UsbPrinterStatusReader
    {
        internal static void CancelPrint(string deviceInstanceId)
        {
            if (string.IsNullOrWhiteSpace(deviceInstanceId) || !deviceInstanceId.StartsWith(
                "USBPRINT\\", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("A USBPRINT device instance is required.");
            using (var device = new UsbPrintDevice(deviceInstanceId))
            {
                // Parser exit before the class reset; never bulk-write after reset.
                try { device.Write(Encoding.ASCII.GetBytes("\0\0\0\x1b\x01@EJL 1284.4\n@EJL     \n\x1b@\x1b@")); }
                catch (Exception error) { System.Diagnostics.Trace.WriteLine("USB parser exit: " + error.Message); }
                device.SoftReset();
            }
        }

        private static readonly byte[] StatusRequest = { (byte)'s', (byte)'t', 1, 0, 1 };
        private static readonly byte[] StatusHeader = Encoding.ASCII.GetBytes("@BDC ST2\r\n");

        internal static List<UsbInkLevel> ReadInk(string deviceInstanceId, bool exclusive = false) =>
            ParseInk(ReadStatus(deviceInstanceId, exclusive));

        internal static byte[] ReadStatus(string deviceInstanceId, bool exclusive = false)
        {
            if (string.IsNullOrWhiteSpace(deviceInstanceId) || !deviceInstanceId.StartsWith(
                "USBPRINT\\", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("A USBPRINT device instance is required.", nameof(deviceInstanceId));
            using (var device = new UsbPrintDevice(deviceInstanceId, exclusive))
            {
                device.Drain();
                return ReadStatus(device.Write, device.Read);
            }
        }

        // IEEE 1284.4 wire layout follows escputil/d4lib (Jean-Jacques
        // Sarton, GPL-2.0-or-later). Keep status off the raw print channel.
        internal static byte[] ReadStatus(Action<byte[]> write, Func<byte[]> read)
        {
            return new ControlChannel(write, read).Query();
        }

        internal static PrinterInformationReport ReadInformation(string deviceInstanceId)
        {
            if (string.IsNullOrWhiteSpace(deviceInstanceId) || !deviceInstanceId.StartsWith(
                "USBPRINT\\", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("A USBPRINT device instance is required.", nameof(deviceInstanceId));
            using (var device = new UsbPrintDevice(deviceInstanceId))
            {
                device.Drain();
                return ReadInformation(device.Write, device.Read, deviceInstanceId);
            }
        }

        internal static PrinterInformationReport ReadInformation(Action<byte[]> write, Func<byte[]> read,
            string source = "USB")
        {
            var report = new PrinterInformationReport { Source = source };
            var clock = System.Diagnostics.Stopwatch.StartNew();
            try
            {
                new ControlChannel(write, read).Query(ask =>
                {
                    foreach (var query in PrinterInformationDecoder.Queries)
                    {
                        if (clock.ElapsedMilliseconds >= 60000) return;
                        try { report.Add(query, ask(PrinterInformationDecoder.Request(query))); }
                        catch (Exception error) { report.AddFailure(query, error.Message); return; }
                    }
                    var status = report.Replies.FirstOrDefault(item => item.Command == "st");
                    var slots = status == null ? 8 : ParseInk(PrinterInformationDecoder.Bytes(status.Hex)).Count;
                    for (var slot = 1; slot <= Math.Min(32, Math.Max(1, slots)); slot++)
                    {
                        if (clock.ElapsedMilliseconds >= 60000) return;
                        var query = "ii:" + slot.ToString(CultureInfo.InvariantCulture);
                        try
                        {
                            var reply = ask(PrinterInformationDecoder.Request(query));
                            report.Add(query, reply);
                            if (Encoding.ASCII.GetString(reply).Contains(":NA;")) break;
                        }
                        catch (Exception error) { report.AddFailure(query, error.Message); return; }
                    }
                });
            }
            catch (Exception error)
            {
                if (!report.Replies.Any(item => !string.IsNullOrEmpty(item.Hex))) throw;
                report.AddFailure("session", error.Message);
            }
            return report;
        }

        private sealed class ControlChannel
        {
            private readonly Action<byte[]> write;
            private readonly Func<byte[]> read;
            private readonly Queue<byte> received = new Queue<byte>();
            private readonly System.Diagnostics.Stopwatch elapsed = System.Diagnostics.Stopwatch.StartNew();
            private byte socket;
            private byte revision = 0x10;

            internal ControlChannel(Action<byte[]> write, Func<byte[]> read)
            {
                this.write = write;
                this.read = read;
            }

            private void CheckDeadline()
            {
                if (elapsed.ElapsedMilliseconds >= 8000)
                    throw new TimeoutException("The USB printer did not complete its EPSON-CTRL status exchange.");
            }

            private byte[] Take(int count)
            {
                var result = new byte[count];
                for (var i = 0; i < count; i++)
                {
                    while (received.Count == 0)
                    {
                        CheckDeadline();
                        var chunk = read();
                        if (chunk == null)
                            throw new InvalidOperationException("Missing IEEE 1284.4 USB transfer.");
                        // USB can complete with a zero-length packet before the next D4 frame.
                        if (chunk.Length == 0)
                        {
                            System.Threading.Thread.Sleep(10);
                            continue;
                        }
                        foreach (var value in chunk) received.Enqueue(value);
                    }
                    result[i] = received.Dequeue();
                }
                return result;
            }

            private byte[] Packet()
            {
                var header = Take(6);
                var length = header[2] * 256 + header[3];
                if (length < 6)
                    throw new InvalidOperationException("Invalid IEEE 1284.4 packet length.");
                return header.Concat(Take(length - 6)).ToArray();
            }

            private byte[] Command(byte command, params byte[] payload)
                => ExchangeCommand(command, payload);

            private byte[] ExchangeCommand(byte command, byte[] payload, bool allowError = false)
            {
                CheckDeadline();
                var length = 7 + payload.Length;
                write(new byte[] { 0, 0, (byte)(length >> 8), (byte)length, 1, 0, command }
                    .Concat(payload).ToArray());
                var reply = Packet();
                if (reply.Length < 8 || reply[0] != 0 || reply[1] != 0 ||
                    reply[6] != (command | 0x80) || (reply[7] != 0 && !allowError))
                    throw new InvalidOperationException("The USB printer rejected IEEE 1284.4 command " + command + ".");
                return reply;
            }

            internal byte[] Query(Action<Func<byte[], byte[]>> collect = null)
            {
                var entered = false;
                var opened = false;
                var failed = false;
                try
                {
                    write(new byte[] { 0, 0, 0, 27, 1 }.Concat(
                        Encoding.ASCII.GetBytes("@EJL 1284.4\n@EJL\n@EJL\n")).ToArray());
                    entered = true;
                    Packet(); // EnterIEEE acknowledgement, before Init.
                    var init = ExchangeCommand(0, new[] { revision }, allowError: true);
                    if (init.Length == 9 && init[7] != 0 && init[8] == 0x20)
                    {
                        revision = 0x20;
                        init = Command(0, revision);
                    }
                    if (init.Length != 9 || init[7] != 0 || init[8] != revision)
                        throw new InvalidOperationException("Unsupported IEEE 1284.4 revision.");
                    var service = Command(9, Encoding.ASCII.GetBytes("EPSON-CTRL"));
                    if (service.Length < 9 || service[8] == 0)
                        throw new InvalidOperationException("EPSON-CTRL is unavailable.");
                    socket = service[8];
                    var channel = revision == 0x10
                        ? Command(1, socket, socket, 2, 0, 2, 0, 0, 0, 0, 0)
                        : Command(1, socket, socket, 2, 0, 2, 0, 0, 0);
                    opened = true;
                    if (channel.Length != (revision == 0x10 ? 16 : 14) || channel[8] != socket || channel[9] != socket ||
                        channel[10] * 256 + channel[11] < 11 || channel[12] * 256 + channel[13] <= 6)
                        throw new InvalidOperationException("Invalid EPSON-CTRL packet sizes.");
                    if (collect == null) return ReadCommand(StatusRequest);
                    collect(ReadCommand);
                    return new byte[0];
                }
                catch { failed = true; throw; }
                finally
                {
                    // Always attempt Exit, including when CloseChannel fails.
                    try
                    {
                        elapsed.Restart();
                        try { if (opened) Command(2, socket, socket, 0); }
                        finally { elapsed.Restart(); if (entered) Command(8); }
                    }
                    catch (Exception) when (failed) { /* Preserve the original query failure. */ }
                }
            }

            private byte[] ReadCommand(byte[] request)
            {
                elapsed.Restart();
                byte[] credit;
                do
                {
                    credit = revision == 0x10
                        ? Command(4, socket, socket, 0, 0x80, 0xff, 0xff)
                        : Command(4, socket, socket, 0, 8);
                    if (credit.Length != 12 || credit[8] != socket || credit[9] != socket)
                        throw new InvalidOperationException("Invalid EPSON-CTRL credit reply.");
                } while (credit[10] == 0 && credit[11] == 0);
                GrantCredit();
                var length = request.Length + 6;
                write(new byte[] { socket, socket, (byte)(length >> 8), (byte)length, 0, 1 }
                    .Concat(request).ToArray());
                var status = new List<byte>();
                var bdc = request.SequenceEqual(StatusRequest);
                while (true)
                {
                    CheckDeadline();
                    var packet = Packet();
                    if (packet[0] != socket || packet[1] != socket)
                        throw new InvalidOperationException("Unexpected EPSON-CTRL data socket.");
                    status.AddRange(packet.Skip(6));
                    if (bdc && status.Count > 0 && status[0] == 0) status.RemoveAt(0);
                    if (status.Count > 65547)
                        throw new InvalidOperationException("EPSON-CTRL reply is too large.");
                    if (bdc && !StatusHeader.Take(Math.Min(10, status.Count)).SequenceEqual(status.Take(10)))
                        throw new InvalidOperationException("Invalid EPSON-CTRL status header.");
                    if (bdc && status.Count >= 12)
                    {
                        if (!StatusHeader.SequenceEqual(status.Take(10)))
                            throw new InvalidOperationException("Invalid EPSON-CTRL status header.");
                        var expected = 12 + status[10] + 256 * status[11];
                        if (status.Count >= expected) return status.Take(expected).ToArray();
                    }
                    if (!bdc && (status.Contains(12) || (packet[5] & 1) != 0)) return status.ToArray();
                    GrantCredit();
                }
            }

            private void GrantCredit()
            {
                var reply = Command(3, socket, socket, 0, 1);
                if (reply.Length != 10 || reply[8] != socket || reply[9] != socket)
                    throw new InvalidOperationException("Invalid EPSON-CTRL receive credit reply.");
            }
        }

        private static List<UsbInkLevel> ParseInk(byte[] reply)
        {
            var inks = new List<UsbInkLevel>();
            if (reply != null && reply.Length > 0 && reply[0] == 0)
                reply = reply.Skip(1).ToArray();
            if (reply == null || reply.Length < 12 || !StatusHeader.SequenceEqual(reply.Take(StatusHeader.Length)))
                throw new InvalidOperationException("The USB printer did not return an Epson BDC status reply.");
            var bodyLength = reply[10] | reply[11] << 8;
            if (bodyLength > reply.Length - 12)
                throw new InvalidOperationException("The USB printer returned an incomplete status reply.");
            var end = 12 + bodyLength;
            var entries = new List<int>();
            for (var position = 12; position < end;)
            {
                if (end - position < 2)
                    throw new InvalidOperationException("The USB printer returned an invalid status reply.");
                var tag = reply[position++];
                var length = reply[position++];
                if (position + length > end)
                    throw new InvalidOperationException("The USB printer returned an invalid status reply.");
                if (tag == 0x0f)
                {
                    if (length < 1)
                        throw new InvalidOperationException("The USB printer returned an invalid ink field.");
                    var width = reply[position];
                    if (width < 3 || (length - 1) % width != 0)
                        throw new InvalidOperationException("The USB printer returned an invalid ink record width.");
                    for (var entry = position + 1; entry + width <= position + length; entry += width)
                        entries.Add(entry);
                }
                position += length;
            }
            var matte = entries.Any(entry => reply[entry] == 0x0b);
            foreach (var entry in entries)
            {
                var level = reply[entry + 2] == (byte)'i' ? 0 : reply[entry + 2];
                inks.Add(new UsbInkLevel
                {
                    Name = InkName(reply[entry], matte),
                    Percent = level <= 100 ? level : -1,
                });
            }
            return inks;
        }

        private static string InkName(byte kind, bool hasMatteBlack)
        {
            switch (kind)
            {
                case 0x01: return hasMatteBlack ? "PK" : "K";
                case 0x03: return "C";
                case 0x04: return "M";
                case 0x05: return "Y";
                case 0x06: return "LC";
                case 0x07: return "LM";
                case 0x0a: return "LK";
                case 0x0b: return "MK";
                case 0x0c: return "R";
                case 0x0d: return "B";
                case 0x0e: return "GO";
                case 0x0f: return "LLK";
                case 0x10: return "OR";
                case 0x11: return "G";
                case 0x16: return "LK";
                default: return "Ink " + kind.ToString("X2", CultureInfo.InvariantCulture);
            }
        }

        private sealed class UsbPrintDevice : IDisposable
        {
            private const uint DigcfPresent = 0x2;
            private const uint DigcfDeviceInterface = 0x10;
            private const uint GenericRead = 0x80000000;
            private const uint GenericWrite = 0x40000000;
            private const uint FileShareRead = 0x1;
            private const uint FileShareWrite = 0x2;
            private const uint OpenExisting = 3;
            private const uint FileFlagOverlapped = 0x40000000;
            private const int ErrorIoPending = 997;
            private const uint WaitObject0 = 0;
            private const uint WaitTimeout = 258;
            private static readonly Guid UsbPrintInterface = new Guid("28d78fad-5a12-11d1-ae5b-0000f803a8c2");
            private static readonly IntPtr InvalidHandleValue = new IntPtr(-1);
            private readonly IntPtr handle;

            internal UsbPrintDevice(string deviceInstanceId, bool exclusive = false)
            {
                handle = CreateFileW(FindInterfacePath(deviceInstanceId), GenericRead | GenericWrite,
                    exclusive ? 0 : FileShareRead | FileShareWrite,
                    IntPtr.Zero, OpenExisting, FileFlagOverlapped, IntPtr.Zero);
                if (handle == InvalidHandleValue)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not open the USB printer interface.");
            }

            internal void Write(byte[] data)
            {
                var offset = 0;
                while (offset < data.Length)
                {
                    var count = Transfer(data.Skip(offset).ToArray(), true);
                    if (count == 0) throw new InvalidOperationException("The USB printer accepted no status data.");
                    offset += count;
                }
            }

            internal byte[] Read()
            {
                // Read a whole USB transfer; ControlChannel buffers D4 frames.
                var buffer = new byte[65536];
                var count = Transfer(buffer, false);
                return buffer.Take(count).ToArray();
            }

            internal void Drain()
            {
                var timer = System.Diagnostics.Stopwatch.StartNew();
                var buffer = new byte[65536];
                while (timer.ElapsedMilliseconds < 300)
                {
                    try { if (Transfer(buffer, false, 100) == 0) break; }
                    catch (TimeoutException) { break; }
                }
            }

            internal void SoftReset() => Transfer(new byte[0], false, 3000, true);

            private int Transfer(byte[] buffer, bool write, uint timeout = 3000, bool reset = false)
            {
                var eventHandle = CreateEventW(IntPtr.Zero, true, false, null);
                if (eventHandle == IntPtr.Zero)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not create USB status event.");
                var pinnedBuffer = GCHandle.Alloc(buffer, GCHandleType.Pinned);
                var overlapped = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(NativeOverlapped)));
                try
                {
                    Marshal.StructureToPtr(new NativeOverlapped { EventHandle = eventHandle }, overlapped, false);
                    uint transferred;
                    var succeeded = reset
                        ? DeviceIoControl(handle, 0x220040, IntPtr.Zero, 0, IntPtr.Zero, 0, out transferred, overlapped)
                        : write
                        ? WriteFile(handle, pinnedBuffer.AddrOfPinnedObject(), (uint)buffer.Length,
                            out transferred, overlapped)
                        : ReadFile(handle, pinnedBuffer.AddrOfPinnedObject(), (uint)buffer.Length,
                            out transferred, overlapped);
                    if (!succeeded)
                    {
                        var error = Marshal.GetLastWin32Error();
                        if (error != ErrorIoPending)
                            throw new Win32Exception(error, "USB printer status transfer failed.");
                        var result = WaitForSingleObject(eventHandle, timeout);
                        if (result != WaitObject0)
                        {
                            var waitError = Marshal.GetLastWin32Error();
                            CancelIoEx(handle, overlapped);
                            // Cancellation is asynchronous: both native OVERLAPPED
                            // and the pinned buffer must survive its completion.
                            GetOverlappedResult(handle, overlapped, out transferred, true);
                            if (result == WaitTimeout)
                                throw new TimeoutException("The USB printer did not answer its status request.");
                            throw new Win32Exception(waitError, "USB status wait failed.");
                        }
                        if (!GetOverlappedResult(handle, overlapped,
                            out transferred, false))
                            throw new Win32Exception(Marshal.GetLastWin32Error(), "USB printer status transfer failed.");
                    }
                    return checked((int)transferred);
                }
                finally
                {
                    Marshal.FreeHGlobal(overlapped);
                    pinnedBuffer.Free();
                    CloseHandle(eventHandle);
                }
            }

            private static string FindInterfacePath(string deviceInstanceId)
            {
                uint node;
                if (CM_Locate_DevNodeW(out node, deviceInstanceId, 0) != 0)
                    throw new InvalidOperationException("The selected USB printer is disconnected.");
                uint parent;
                if (CM_Get_Parent(out parent, node, 0) != 0)
                    throw new InvalidOperationException("The USB printer has no USB parent interface.");
                uint length;
                if (CM_Get_Device_ID_Size(out length, parent, 0) != 0)
                    throw new InvalidOperationException("Could not read the USB printer parent identity.");
                var parentId = new StringBuilder(checked((int)length + 1));
                if (CM_Get_Device_IDW(parent, parentId, length + 1, 0) != 0)
                    throw new InvalidOperationException("Could not read the USB printer parent identity.");
                var interfaceGuid = UsbPrintInterface;
                var devices = SetupDiGetClassDevsW(ref interfaceGuid, null, IntPtr.Zero,
                    DigcfPresent | DigcfDeviceInterface);
                if (devices == InvalidHandleValue)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not enumerate USB printer interfaces.");
                try
                {
                    for (uint index = 0; ; index++)
                    {
                        var interfaceData = new SpDeviceInterfaceData { cbSize = Marshal.SizeOf(typeof(SpDeviceInterfaceData)) };
                        if (!SetupDiEnumDeviceInterfaces(devices, IntPtr.Zero, ref interfaceGuid,
                            index, ref interfaceData))
                        {
                            var error = Marshal.GetLastWin32Error();
                            if (error == 259) break;
                            throw new Win32Exception(error, "Could not enumerate USB printer interfaces.");
                        }
                        uint required;
                        var info = new SpDevInfoData { cbSize = Marshal.SizeOf(typeof(SpDevInfoData)) };
                        SetupDiGetDeviceInterfaceDetailW(devices, ref interfaceData, IntPtr.Zero, 0,
                            out required, ref info);
                        if (required == 0 || required > 65536)
                            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read USB printer interface size.");
                        var detail = Marshal.AllocHGlobal(checked((int)required));
                        try
                        {
                            Marshal.WriteInt32(detail, IntPtr.Size == 8 ? 8 : 6);
                            if (!SetupDiGetDeviceInterfaceDetailW(devices, ref interfaceData, detail,
                                required, out required, ref info))
                                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read USB printer interface.");
                            var instanceId = new StringBuilder(1024);
                            if (!SetupDiGetDeviceInstanceIdW(devices, ref info, instanceId,
                                instanceId.Capacity, IntPtr.Zero))
                                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read USB printer interface identity.");
                            if (string.Equals(instanceId.ToString(), parentId.ToString(), StringComparison.OrdinalIgnoreCase))
                                return Marshal.PtrToStringUni(IntPtr.Add(detail, 4));
                        }
                        finally { Marshal.FreeHGlobal(detail); }
                    }
                }
                finally { SetupDiDestroyDeviceInfoList(devices); }
                throw new InvalidOperationException("The USB printer interface was not found.");
            }

            public void Dispose()
            {
                if (handle != IntPtr.Zero && handle != InvalidHandleValue) CloseHandle(handle);
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct SpDeviceInterfaceData
            {
                public int cbSize;
                public Guid InterfaceClassGuid;
                public int Flags;
                public IntPtr Reserved;
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct SpDevInfoData
            {
                public int cbSize;
                public Guid ClassGuid;
                public int DevInst;
                public IntPtr Reserved;
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct NativeOverlapped
            {
                public IntPtr Internal;
                public IntPtr InternalHigh;
                public int Offset;
                public int OffsetHigh;
                public IntPtr EventHandle;
            }

            [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
            private static extern uint CM_Locate_DevNodeW(out uint node, string id, uint flags);
            [DllImport("cfgmgr32.dll")]
            private static extern uint CM_Get_Parent(out uint parent, uint node, uint flags);
            [DllImport("cfgmgr32.dll")]
            private static extern uint CM_Get_Device_ID_Size(out uint length, uint node, uint flags);
            [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
            private static extern uint CM_Get_Device_IDW(uint node, StringBuilder value, uint length, uint flags);
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern IntPtr SetupDiGetClassDevsW(ref Guid guid, string enumerator,
                IntPtr parent, uint flags);
            [DllImport("setupapi.dll", SetLastError = true)]
            private static extern bool SetupDiEnumDeviceInterfaces(IntPtr devices, IntPtr data,
                ref Guid guid, uint index, ref SpDeviceInterfaceData interfaceData);
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern bool SetupDiGetDeviceInterfaceDetailW(IntPtr devices,
                ref SpDeviceInterfaceData interfaceData, IntPtr detail, uint detailSize,
                out uint requiredSize, ref SpDevInfoData deviceInfoData);
            [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern bool SetupDiGetDeviceInstanceIdW(IntPtr devices,
                ref SpDevInfoData deviceInfoData, StringBuilder instanceId, int capacity,
                IntPtr requiredSize);
            [DllImport("setupapi.dll", SetLastError = true)]
            private static extern bool SetupDiDestroyDeviceInfoList(IntPtr devices);
            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern IntPtr CreateFileW(string name, uint access, uint share,
                IntPtr security, uint creation, uint flags, IntPtr template);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool DeviceIoControl(IntPtr handle, uint code, IntPtr input, uint inputSize,
                IntPtr output, uint outputSize, out uint returned, IntPtr overlapped);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool ReadFile(IntPtr handle, IntPtr buffer, uint count,
                out uint read, IntPtr overlapped);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool WriteFile(IntPtr handle, IntPtr buffer, uint count,
                out uint written, IntPtr overlapped);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool GetOverlappedResult(IntPtr handle,
                IntPtr overlapped, out uint transferred, bool wait);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool CancelIoEx(IntPtr handle, IntPtr overlapped);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern IntPtr CreateEventW(IntPtr attributes, bool manualReset,
                bool initialState, string name);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool CloseHandle(IntPtr handle);
        }
    }
}
