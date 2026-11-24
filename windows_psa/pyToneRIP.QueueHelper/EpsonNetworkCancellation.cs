// Copyright (C) 2026 Alexander Eichhorn
// SPDX-License-Identifier: GPL-3.0-only
using System;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace pyToneRIP.Cancellation
{
    // EPSON-CTRL rs/Initialize is a mutation despite its SNMP GET envelope.
    // Only a validated BUSY response permits another attempt.
    internal static class EpsonNetworkCancellation
    {
        private static readonly byte[] Community = Encoding.ASCII.GetBytes("public");
        private static readonly byte[] Oid = {43,6,1,4,1,0x89,0x60,1,2,2,44,1,1,2,1,114,115,1,0,1};
        private static readonly byte[] PrinterStatusOid = {43,6,1,2,1,25,3,5,1,1};

        // Read-only hrPrinterStatus, also used by the existing network status
        // service. This gates cancellation after delivery; no extra UI status.
        internal static bool IsPrinting(string host, int port = 161)
        {
            if (string.IsNullOrWhiteSpace(host)) return false;
            try
            {
                var id = Environment.TickCount & 0x3fffffff;
                var binding = Tlv(0x30, Join(Tlv(6, PrinterStatusOid), Tlv(5, new byte[0])));
                var packet = Tlv(0x30, Join(Integer(0), Tlv(4, Community),
                    Tlv(0xa1, Join(Integer(id), Integer(0), Integer(0), Tlv(0x30, binding)))));
                using (var client = new UdpClient())
                {
                    client.Client.ReceiveTimeout = 1000;
                    client.Connect(host, port);
                    client.Send(packet, packet.Length);
                    System.Net.IPEndPoint peer = null;
                    var envelope = new Reader(client.Receive(ref peer));
                    var message = new Reader(envelope.Take(0x30)); envelope.End();
                    if (message.Number() != 0 || !message.Take(4).SequenceEqual(Community)) return false;
                    var pdu = new Reader(message.Take(0xa2)); message.End();
                    if (pdu.Number() != id || pdu.Number() != 0 || pdu.Number() != 0) return false;
                    var bindings = new Reader(pdu.Take(0x30)); pdu.End();
                    var value = new Reader(bindings.Take(0x30)); bindings.End();
                    var oid = value.Take(6);
                    if (oid.Length <= PrinterStatusOid.Length || !oid.Take(PrinterStatusOid.Length).SequenceEqual(PrinterStatusOid)) return false;
                    var status = value.Number(); value.End();
                    return status == 4;
                }
            }
            catch (SocketException) { return false; }
            catch (IOException) { return false; }
            catch (OverflowException) { return false; }
        }

        internal static void Cancel(string host, int port = 161)
        {
            if (string.IsNullOrWhiteSpace(host)) throw new ArgumentException("A printer host is required.");
            var binding = Tlv(0x30, Join(Tlv(6, Oid), Tlv(5, new byte[0])));
            var random = new byte[4];
            using (var generator = RandomNumberGenerator.Create())
            using (var client = new UdpClient())
            {
                client.Client.ReceiveTimeout = 2000;
                client.Connect(host, port);
                for (var attempt = 0; attempt < 3; attempt++)
                {
                    if (attempt != 0) Thread.Sleep(500);
                    generator.GetBytes(random);
                    var id = BitConverter.ToInt32(random, 0) & 0x3fffffff;
                    var packet = Tlv(0x30, Join(Integer(0), Tlv(4, Community),
                        Tlv(0xa0, Join(Integer(id), Integer(0), Integer(0), Tlv(0x30, binding)))));
                    client.Send(packet, packet.Length);
                    System.Net.IPEndPoint peer = null;
                    if (Validate(client.Receive(ref peer), id)) return;
                }
            }
            throw new IOException("The printer continued to return BUSY for physical print cancellation.");
        }

        // False means that this exact request received the explicit BUSY reply.
        internal static bool Validate(byte[] reply, int id)
        {
            var envelope = new Reader(reply);
            var message = new Reader(envelope.Take(0x30)); envelope.End();
            if (message.Number() != 0 || !message.Take(4).SequenceEqual(Community))
                throw new InvalidDataException("Unrelated cancellation response.");
            var pdu = new Reader(message.Take(0xa2)); message.End();
            if (pdu.Number() != id || pdu.Number() != 0 || pdu.Number() != 0)
                throw new InvalidDataException("Printer rejected the cancellation request.");
            var bindings = new Reader(pdu.Take(0x30)); pdu.End();
            var binding = new Reader(bindings.Take(0x30)); bindings.End();
            if (!binding.Take(6).SequenceEqual(Oid)) throw new InvalidDataException("Unrelated cancellation OID.");
            var value = binding.Take(4); binding.End();
            var text = Encoding.ASCII.GetString(value);
            if (text == "\0rs:01:OK;\f" || text == "rs:01:OK;\f") return true;
            if (text == "\0rs:01:BUSY;\f" || text == "rs:01:BUSY;\f") return false;
            throw new IOException("The printer did not acknowledge physical print cancellation. Response="
                + BitConverter.ToString(value.Take(64).ToArray()).Replace("-", ""));
        }

        private static byte[] Join(params byte[][] values) => values.SelectMany(value => value).ToArray();
        private static byte[] Integer(int value)
        {
            var bytes = new[] {(byte)(value >> 24), (byte)(value >> 16), (byte)(value >> 8), (byte)value};
            int start = 0;
            while (start < 3 && bytes[start] == 0 && bytes[start + 1] < 128) start++;
            return Tlv(2, bytes.Skip(start).ToArray());
        }
        private static byte[] Tlv(byte tag, byte[] value)
        {
            var length = value.Length < 128 ? new[] {(byte)value.Length}
                : new[] {(byte)0x82, (byte)(value.Length >> 8), (byte)value.Length};
            return Join(new[] {tag}, length, value);
        }
        private sealed class Reader
        {
            private readonly byte[] bytes;
            private int offset;
            internal Reader(byte[] bytes) { this.bytes = bytes; }
            internal void End() { if (offset != bytes.Length) throw new InvalidDataException("Trailing SNMP data."); }
            internal byte[] Take(byte tag)
            {
                if (offset + 2 > bytes.Length || bytes[offset++] != tag) throw new InvalidDataException("Invalid SNMP element.");
                int length = bytes[offset++];
                if (length >= 128)
                {
                    int count = length & 127; length = 0;
                    if (count < 1 || count > 2 || offset + count > bytes.Length) throw new InvalidDataException("Invalid SNMP length.");
                    while (count-- > 0) length = (length << 8) | bytes[offset++];
                }
                if (offset + length > bytes.Length) throw new InvalidDataException("Truncated SNMP response.");
                var result = new byte[length]; Array.Copy(bytes, offset, result, 0, length); offset += length;
                return result;
            }
            internal int Number()
            {
                var value = Take(2);
                if (value.Length < 1 || value.Length > 4 || value[0] >= 128) throw new InvalidDataException("Invalid SNMP integer.");
                int result = 0;
                foreach (var item in value) result = checked(result * 256 + item);
                return result;
            }
        }
    }
}
