using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using pyToneRIP.Models;

namespace pyToneRIP.QueueHelper
{
    internal static class UsbPrinterStatusReader
    {
        private static readonly byte[] StatusRequest = { (byte)'s', (byte)'t', 1, 0, 1 };
        private static readonly byte[] StatusHeader = Encoding.ASCII.GetBytes("@BDC ST2\r\n");

        internal static List<UsbInkLevel> ReadInk(string deviceInstanceId)
        {
            if (string.IsNullOrWhiteSpace(deviceInstanceId) || !deviceInstanceId.StartsWith(
                "USBPRINT\\", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("A USBPRINT device instance is required.", nameof(deviceInstanceId));
            using (var device = new UsbPrintDevice(deviceInstanceId))
            {
                device.Write(StatusRequest);
                return ParseInk(device.Read());
            }
        }

        private static List<UsbInkLevel> ParseInk(byte[] reply)
        {
            var inks = new List<UsbInkLevel>();
            if (reply == null || reply.Length < 12 || !StatusHeader.SequenceEqual(reply.Take(StatusHeader.Length)))
                throw new InvalidOperationException("The USB printer did not return an Epson BDC status reply.");
            var bodyLength = reply[10] | reply[11] << 8;
            if (bodyLength > reply.Length - 12)
                throw new InvalidOperationException("The USB printer returned an incomplete status reply.");
            var end = 12 + bodyLength;
            for (var position = 12; position + 2 <= end;)
            {
                var tag = reply[position++];
                var length = reply[position++];
                if (position + length > end)
                    throw new InvalidOperationException("The USB printer returned an invalid status reply.");
                if (tag == 0x0f && length >= 4)
                {
                    var width = reply[position];
                    if ((width == 3 || width == 4) && (length - 1) % width == 0)
                    {
                        var kinds = new HashSet<byte>();
                        for (var entry = position + 1; entry + width <= position + length; entry += width)
                            kinds.Add(reply[entry]);
                        for (var entry = position + 1; entry + width <= position + length; entry += width)
                        {
                            var level = reply[entry + 2] == (byte)'i' ? 0 : reply[entry + 2];
                            if (level <= 100) inks.Add(new UsbInkLevel
                            {
                                Name = InkName(reply[entry], kinds.Contains(0x0b)),
                                Percent = level,
                            });
                        }
                    }
                }
                position += length;
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
                case 0x0e: return "O";
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

            internal UsbPrintDevice(string deviceInstanceId)
            {
                handle = CreateFileW(FindInterfacePath(deviceInstanceId), GenericRead | GenericWrite,
                    FileShareRead | FileShareWrite, IntPtr.Zero, OpenExisting, FileFlagOverlapped, IntPtr.Zero);
                if (handle == InvalidHandleValue)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not open the USB printer interface.");
            }

            internal void Write(byte[] data) => Transfer(data, true);

            internal byte[] Read()
            {
                var buffer = new byte[8192];
                var count = Transfer(buffer, false);
                if (count == 0) throw new InvalidOperationException("The USB printer returned an empty status reply.");
                return buffer.Take(count).ToArray();
            }

            private int Transfer(byte[] buffer, bool write)
            {
                var eventHandle = CreateEventW(IntPtr.Zero, true, false, null);
                if (eventHandle == IntPtr.Zero)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not create USB status event.");
                var pinnedBuffer = GCHandle.Alloc(buffer, GCHandleType.Pinned);
                try
                {
                    var overlapped = new NativeOverlapped { EventHandle = eventHandle };
                    uint transferred;
                    var succeeded = write
                        ? WriteFile(handle, pinnedBuffer.AddrOfPinnedObject(), (uint)buffer.Length,
                            out transferred, ref overlapped)
                        : ReadFile(handle, pinnedBuffer.AddrOfPinnedObject(), (uint)buffer.Length,
                            out transferred, ref overlapped);
                    if (!succeeded)
                    {
                        var error = Marshal.GetLastWin32Error();
                        if (error != ErrorIoPending)
                            throw new Win32Exception(error, "USB printer status transfer failed.");
                        var result = WaitForSingleObject(eventHandle, 3000);
                        if (result == WaitTimeout)
                        {
                            CancelIoEx(handle, ref overlapped);
                            throw new TimeoutException("The USB printer did not answer its status request.");
                        }
                        if (result != WaitObject0 || !GetOverlappedResult(handle, ref overlapped,
                            out transferred, false))
                            throw new Win32Exception(Marshal.GetLastWin32Error(), "USB printer status transfer failed.");
                    }
                    return checked((int)transferred);
                }
                finally
                {
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
            private static extern bool ReadFile(IntPtr handle, IntPtr buffer, uint count,
                out uint read, ref NativeOverlapped overlapped);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool WriteFile(IntPtr handle, IntPtr buffer, uint count,
                out uint written, ref NativeOverlapped overlapped);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool GetOverlappedResult(IntPtr handle,
                ref NativeOverlapped overlapped, out uint transferred, bool wait);
            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool CancelIoEx(IntPtr handle, ref NativeOverlapped overlapped);
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
