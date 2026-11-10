//! ArcHxv4.cs -- KiriKiri XP3 reader for the Cxdec/Hxv4 protection.
//
// 目的只有一個：讓 GARbro 能正確顯示這類遊戲的真實檔名並取出正確內容。
//
// 這類遊戲（柚子社 / Hikari Field，例如 天使☆騒々 RE-BOOT!）的 XP3 有三層保護：
//
//   1. index 裡的檔名全部被換成 U+5000+id 的假名（倀、倁、倂…），
//      所以 GARbro 原始版本只會列出這些亂碼字。
//   2. index 末端多一個 `Hxv4` chunk，是 XChaCha20-Poly1305 加密的
//      「BLAKE2s(檔名) → (archive_slot, entry_index, key)」對照表。
//      真正的檔名不在裡面，只有雜湊。
//   3. 每個 entry 的明文都被 DripValue VM 產生的 48-byte seed 做串流 XOR，
//      再用 Adler32 校驗。
//
// 本檔案在託管碼裡實作 2 與 3，並用外部 `HxNames.lst`（hash:name）把雜湊
// 換回真實檔名。`HxNames.lst` 與 `drip_program.json` 會優先在
// `GARbro/GameData/` 找，其次才是 DLL 同層與 `hxv4/`（舊版相容）。
//
// 建置：見同目錄 build.ps1

using System;
using System.Collections.Generic;
using System.ComponentModel.Composition;
using System.IO;
using System.Linq;
using System.Text;
using Newtonsoft.Json.Linq;

namespace GameRes.Formats.Hxv4
{
    // ==================================================================
    // XChaCha20-Poly1305（只做解密 + 驗證，Hxv4 payload 用）
    // ==================================================================
    static class ChaCha
    {
        static uint Rotl (uint v, int c) { return (v << c) | (v >> (32 - c)); }

        static void Qr (uint[] s, int a, int b, int c, int d)
        {
            s[a] += s[b]; s[d] = Rotl (s[d] ^ s[a], 16);
            s[c] += s[d]; s[b] = Rotl (s[b] ^ s[c], 12);
            s[a] += s[b]; s[d] = Rotl (s[d] ^ s[a], 8);
            s[c] += s[d]; s[b] = Rotl (s[b] ^ s[c], 7);
        }

        public static void Block (byte[] key, uint counter, byte[] nonce12, byte[] dst)
        {
            var st = new uint[16];
            st[0] = 0x61707865; st[1] = 0x3320646e;
            st[2] = 0x79622d32; st[3] = 0x6b206574;
            for (int i = 0; i < 8; ++i)
                st[4 + i] = BitConverter.ToUInt32 (key, i * 4);
            st[12] = counter;
            for (int i = 0; i < 3; ++i)
                st[13 + i] = BitConverter.ToUInt32 (nonce12, i * 4);

            var w = (uint[])st.Clone ();
            for (int i = 0; i < 10; ++i)
            {
                Qr (w, 0, 4, 8, 12); Qr (w, 1, 5, 9, 13); Qr (w, 2, 6, 10, 14); Qr (w, 3, 7, 11, 15);
                Qr (w, 0, 5, 10, 15); Qr (w, 1, 6, 11, 12); Qr (w, 2, 7, 8, 13); Qr (w, 3, 4, 9, 14);
            }
            for (int i = 0; i < 16; ++i)
            {
                var v = w[i] + st[i];
                dst[i * 4 + 0] = (byte)v;
                dst[i * 4 + 1] = (byte)(v >> 8);
                dst[i * 4 + 2] = (byte)(v >> 16);
                dst[i * 4 + 3] = (byte)(v >> 24);
            }
        }

        // XChaCha20 的 24-byte nonce 前 16 bytes 先用 HChaCha20 壓成子金鑰
        public static byte[] HChaCha20 (byte[] key, byte[] nonce16)
        {
            var st = new uint[16];
            st[0] = 0x61707865; st[1] = 0x3320646e;
            st[2] = 0x79622d32; st[3] = 0x6b206574;
            for (int i = 0; i < 8; ++i)
                st[4 + i] = BitConverter.ToUInt32 (key, i * 4);
            for (int i = 0; i < 4; ++i)
                st[12 + i] = BitConverter.ToUInt32 (nonce16, i * 4);

            for (int i = 0; i < 10; ++i)
            {
                Qr (st, 0, 4, 8, 12); Qr (st, 1, 5, 9, 13); Qr (st, 2, 6, 10, 14); Qr (st, 3, 7, 11, 15);
                Qr (st, 0, 5, 10, 15); Qr (st, 1, 6, 11, 12); Qr (st, 2, 7, 8, 13); Qr (st, 3, 4, 9, 14);
            }
            var outp = new byte[32];
            int[] idx = { 0, 1, 2, 3, 12, 13, 14, 15 };
            for (int i = 0; i < 8; ++i)
                BitConverter.GetBytes (st[idx[i]]).CopyTo (outp, i * 4);
            return outp;
        }
    }

    static class Poly1305
    {
        const ulong Mask26 = 0x3FFFFFF;

        public static byte[] Tag (byte[] key32, byte[] data)
        {
            ulong r0 = BitConverter.ToUInt32 (key32, 0) & 0x3FFFFFF;
            ulong r1 = (BitConverter.ToUInt32 (key32, 3) >> 2) & 0x3FFFF03;
            ulong r2 = (BitConverter.ToUInt32 (key32, 6) >> 4) & 0x3FFC0FF;
            ulong r3 = (BitConverter.ToUInt32 (key32, 9) >> 6) & 0x3F03FFF;
            ulong r4 = (BitConverter.ToUInt32 (key32, 12) >> 8) & 0x00FFFFF;
            ulong s1 = r1 * 5, s2 = r2 * 5, s3 = r3 * 5, s4 = r4 * 5;

            ulong h0 = 0, h1 = 0, h2 = 0, h3 = 0, h4 = 0;
            var buf = new byte[16];
            int pos = 0;
            while (pos < data.Length)
            {
                int n = Math.Min (16, data.Length - pos);
                Array.Clear (buf, 0, 16);
                Buffer.BlockCopy (data, pos, buf, 0, n);
                if (n < 16)
                    buf[n] = 1;          // 區塊尾端補 1 bit
                pos += n;

                ulong t0 = BitConverter.ToUInt32 (buf, 0) & Mask26;
                ulong t1 = (BitConverter.ToUInt32 (buf, 3) >> 2) & Mask26;
                ulong t2 = (BitConverter.ToUInt32 (buf, 6) >> 4) & Mask26;
                ulong t3 = (BitConverter.ToUInt32 (buf, 9) >> 6) & Mask26;
                ulong t4 = (BitConverter.ToUInt32 (buf, 12) >> 8) & Mask26;
                if (n == 16)
                    t4 |= 1UL << 24;          // 滿區塊的 2^128 位元

                h0 += t0; h1 += t1; h2 += t2; h3 += t3; h4 += t4;

                ulong d0 = h0 * r0 + h1 * s4 + h2 * s3 + h3 * s2 + h4 * s1;
                ulong d1 = h0 * r1 + h1 * r0 + h2 * s4 + h3 * s3 + h4 * s2;
                ulong d2 = h0 * r2 + h1 * r1 + h2 * r0 + h3 * s4 + h4 * s3;
                ulong d3 = h0 * r3 + h1 * r2 + h2 * r1 + h3 * r0 + h4 * s4;
                ulong d4 = h0 * r4 + h1 * r3 + h2 * r2 + h3 * r1 + h4 * r0;

                ulong c = d0 >> 26; h0 = d0 & Mask26;
                d1 += c; c = d1 >> 26; h1 = d1 & Mask26;
                d2 += c; c = d2 >> 26; h2 = d2 & Mask26;
                d3 += c; c = d3 >> 26; h3 = d3 & Mask26;
                d4 += c; c = d4 >> 26; h4 = d4 & Mask26;
                h0 += c * 5; c = h0 >> 26; h0 &= Mask26; h1 += c;
            }

            ulong cc = h1 >> 26; h1 &= Mask26;
            h2 += cc; cc = h2 >> 26; h2 &= Mask26;
            h3 += cc; cc = h3 >> 26; h3 &= Mask26;
            h4 += cc; cc = h4 >> 26; h4 &= Mask26;
            h0 += cc * 5; cc = h0 >> 26; h0 &= Mask26; h1 += cc;

            ulong g0 = h0 + 5; cc = g0 >> 26; g0 &= Mask26;
            ulong g1 = h1 + cc; cc = g1 >> 26; g1 &= Mask26;
            ulong g2 = h2 + cc; cc = g2 >> 26; g2 &= Mask26;
            ulong g3 = h3 + cc; cc = g3 >> 26; g3 &= Mask26;
            ulong g4 = h4 + cc - (1UL << 26);
            if ((g4 & 0x8000000000000000UL) == 0) { h0 = g0; h1 = g1; h2 = g2; h3 = g3; h4 = g4; }

            ulong f0 = (h0 | (h1 << 26)) & 0xFFFFFFFF;
            ulong f1 = ((h1 >> 6) | (h2 << 20)) & 0xFFFFFFFF;
            ulong f2 = ((h2 >> 12) | (h3 << 14)) & 0xFFFFFFFF;
            ulong f3 = ((h3 >> 18) | (h4 << 8)) & 0xFFFFFFFF;

            ulong a0 = f0 + BitConverter.ToUInt32 (key32, 16); f1 += a0 >> 32; a0 &= 0xFFFFFFFF;
            ulong a1 = f1 + BitConverter.ToUInt32 (key32, 20); f2 += a1 >> 32; a1 &= 0xFFFFFFFF;
            ulong a2 = f2 + BitConverter.ToUInt32 (key32, 24); f3 += a2 >> 32; a2 &= 0xFFFFFFFF;
            ulong a3 = (f3 + BitConverter.ToUInt32 (key32, 28)) & 0xFFFFFFFF;

            var tag = new byte[16];
            BitConverter.GetBytes ((uint)a0).CopyTo (tag, 0);
            BitConverter.GetBytes ((uint)a1).CopyTo (tag, 4);
            BitConverter.GetBytes ((uint)a2).CopyTo (tag, 8);
            BitConverter.GetBytes ((uint)a3).CopyTo (tag, 12);
            return tag;
        }
    }

    static class XChaCha20Poly1305
    {
        public static byte[] Decrypt (byte[] key, byte[] nonce24, byte[] payload)
        {
            if (payload.Length < 16)
                throw new InvalidDataException ("Hxv4 payload too short");

            var tag = new byte[16];
            Buffer.BlockCopy (payload, 0, tag, 0, 16);          // 前 16 bytes 是 Poly1305 tag
            var ct = new byte[payload.Length - 16];
            Buffer.BlockCopy (payload, 16, ct, 0, ct.Length);

            var sub = new byte[16];
            Buffer.BlockCopy (nonce24, 0, sub, 0, 16);
            var subKey = ChaCha.HChaCha20 (key, sub);
            var n12 = new byte[12];
            Buffer.BlockCopy (nonce24, 16, n12, 4, 8);

            var block = new byte[64];
            ChaCha.Block (subKey, 0, n12, block);
            var polyKey = new byte[32];
            Buffer.BlockCopy (block, 0, polyKey, 0, 32);

            int pad = (16 - (ct.Length % 16)) % 16;
            var macData = new byte[ct.Length + pad + 16];
            Buffer.BlockCopy (ct, 0, macData, 0, ct.Length);
            BitConverter.GetBytes ((ulong)0).CopyTo (macData, ct.Length + pad);
            BitConverter.GetBytes ((ulong)ct.Length).CopyTo (macData, ct.Length + pad + 8);

            var mac = Poly1305.Tag (polyKey, macData);
            int diff = 0;
            for (int i = 0; i < 16; ++i)
                diff |= mac[i] ^ tag[i];
            if (diff != 0)
                throw new InvalidDataException ("Hxv4 payload authentication failed");

            var pt = new byte[ct.Length];
            uint counter = 1;
            for (int off = 0; off < ct.Length; off += 64, ++counter)
            {
                ChaCha.Block (subKey, counter, n12, block);
                int n = Math.Min (64, ct.Length - off);
                for (int i = 0; i < n; ++i)
                    pt[off + i] = (byte)(ct[off + i] ^ block[i]);
            }
            return pt;
        }
    }

    // ==================================================================
    // DripValue VM：把 record.key 轉成 48-byte filter seed
    // ==================================================================
    class DripProgram
    {
        public uint[] HolderWords;
        public uint[] Context;
        public uint[][,] Lanes;
        public byte[] Hxv4Key;
        public byte[][] Hxv4Nonces;

        const int OP_ADD_IMM = 0x17C50;
        const int OP_RECURSE = 0x17C60;
        const int OP_ADD_SCRATCH = 0x17CB0;
        const int OP_MUL_SCRATCH = 0x17CD0;
        const int OP_SCRATCH_MINUS_RESULT = 0x17CF0;
        const int OP_SHL_SCRATCH = 0x17D10;
        const int OP_SHR_SCRATCH = 0x17D30;
        const int OP_SUB_SCRATCH = 0x17D50;
        const int OP_BIT_SHUFFLE = 0x17D70;
        const int OP_SET_IMM = 0x17DA0;
        const int OP_SET_SEED = 0x17DB0;
        const int OP_DEC = 0x17DD0;
        const int OP_INC = 0x17DE0;
        const int OP_NEG = 0x17DF0;
        const int OP_NOT = 0x17E00;
        const int OP_TABLE_IMM = 0x17E10;
        const int OP_TABLE_MASKED = 0x17E30;
        const int OP_SUB_IMM = 0x17E50;
        const int OP_STORE_SCRATCH = 0x17E60;
        const int OP_XOR_IMM = 0x17E80;
        const int OP_STOP = 0x51D90;

        public static DripProgram Load (string path)
        {
            var root = JObject.Parse (File.ReadAllText (path, Encoding.UTF8));
            var p = new DripProgram ();
            p.HolderWords = ((JArray)root["holder_words"]).Select (x => (uint)(long)x).ToArray ();
            p.Context = ((JArray)root["context_u32"]).Select (x => (uint)(long)x).ToArray ();
            var lanes = (JArray)root["lanes"];
            p.Lanes = new uint[lanes.Count][,];
            for (int i = 0; i < lanes.Count; ++i)
            {
                var recs = (JArray)lanes[i]["records"];
                p.Lanes[i] = new uint[recs.Count, 2];
                for (int j = 0; j < recs.Count; ++j)
                {
                    p.Lanes[i][j, 0] = (uint)(long)recs[j][0];
                    p.Lanes[i][j, 1] = (uint)(long)recs[j][1];
                }
            }
            if (root["hxv4_key"] != null)
                p.Hxv4Key = Hex.Decode ((string)root["hxv4_key"]);
            if (root["hxv4_nonce0"] != null && root["hxv4_nonce1"] != null)
                p.Hxv4Nonces = new[] {
                    Hex.Decode ((string)root["hxv4_nonce0"]),
                    Hex.Decode ((string)root["hxv4_nonce1"]) };
            return p;
        }

        void Eval (uint[,] recs, ref int pc, ref uint result, ref uint scratch, uint seed)
        {
            int n = recs.GetLength (0);
            while (pc < n)
            {
                uint param = recs[pc, 0];
                uint op = recs[pc, 1];
                ++pc;
                if (op == OP_STOP)
                    break;
                if (op == OP_RECURSE)
                {
                    uint r = result, s = scratch;
                    Eval (recs, ref pc, ref r, ref s, seed);
                    result = r;
                    continue;
                }
                unchecked
                {
                    switch (op)
                    {
                    case OP_ADD_IMM: result += param; break;
                    case OP_ADD_SCRATCH: result += scratch; break;
                    case OP_MUL_SCRATCH: result *= scratch; break;
                    case OP_SCRATCH_MINUS_RESULT: result = scratch - result; break;
                    case OP_SHL_SCRATCH: result <<= (int)(scratch & 0xF); break;
                    case OP_SHR_SCRATCH: result >>= (int)(scratch & 0xF); break;
                    case OP_SUB_SCRATCH: result -= scratch; break;
                    case OP_BIT_SHUFFLE:
                        result = (uint)((2u * (result & ~param)) | ((param >> 1) & (result >> 1)));
                        break;
                    case OP_SET_IMM: result = param; break;
                    case OP_SET_SEED: result = seed; break;
                    case OP_DEC: result -= 1; break;
                    case OP_INC: result += 1; break;
                    case OP_NEG: result = (uint)(-(int)result); break;
                    case OP_NOT: result = ~result; break;
                    case OP_TABLE_IMM: result = Context[param]; break;
                    case OP_TABLE_MASKED: result = Context[param & result]; break;
                    case OP_SUB_IMM: result -= param; break;
                    case OP_STORE_SCRATCH: scratch = result; break;
                    case OP_XOR_IMM: result ^= param; break;
                    default:
                        throw new InvalidDataException ("unknown Drip op 0x" + op.ToString ("x"));
                    }
                }
            }
        }

        uint EvalLane (int lane, uint seed)
        {
            uint result = 0, scratch = 0;
            var recs = Lanes[lane];
            int pc = 0;
            Eval (recs, ref pc, ref result, ref scratch, seed);
            return result;
        }

        public ulong Get64FromU32 (uint value)
        {
            int lane = (int)(value & 0x7F);
            uint seed = value >> 7;
            ulong lo = EvalLane (lane, seed);
            ulong hi = EvalLane (lane, ~seed);
            return lo | (hi << 32);
        }

        public byte[] BuildFilterState (ulong key, int openFlag)
        {
            uint keyLo = (uint)key, keyHi = (uint)(key >> 32);
            if ((openFlag & 1) == 0)
            {
                keyLo ^= HolderWords[2];
                keyHi ^= HolderWords[3];
            }
            ulong key64 = keyLo | ((ulong)keyHi << 32);

            var state = new byte[48];
            PutU64 (state, 0, Get64FromU32 (keyLo));
            PutU64 (state, 8, Get64FromU32 (keyHi));
            ulong bulk = (ulong)HolderWords[5] + (HolderWords[4] & (key64 >> 16));
            PutU32 (state, 16, (uint)bulk);
            PutU32 (state, 20, 0);

            ulong cur = ~key64;
            int bitpos = -1;
            int outPos = 24;
            while (outPos < 40)
            {
                if (bitpos < 0)
                {
                    cur = ~Get64FromU32 ((uint)cur);
                    bitpos = 64;
                }
                else
                {
                    state[outPos] = (byte)((cur >> bitpos) & 0xFF);
                    ++outPos;
                }
                bitpos -= 8;
            }
            state[44] = 1;
            state[45] = 0;
            return state;
        }

        static void PutU32 (byte[] b, int o, uint v)
        {
            b[o] = (byte)v; b[o + 1] = (byte)(v >> 8);
            b[o + 2] = (byte)(v >> 16); b[o + 3] = (byte)(v >> 24);
        }

        static void PutU64 (byte[] b, int o, ulong v)
        {
            PutU32 (b, o, (uint)v);
            PutU32 (b, o + 4, (uint)(v >> 32));
        }
    }

    // ==================================================================
    // 內容 filter：48-byte seed -> 明文 XOR
    // ==================================================================
    struct FilterBoundary
    {
        public int Pos0, Pos1;
        public uint Key;
        public byte Byte0, Byte1;
    }

    class FilterState
    {
        FilterBoundary m_b0, m_b1;
        long m_split;
        byte[] m_bulk;

        public FilterState (byte[] seed)
        {
            if (seed.Length != 48)
                throw new InvalidDataException ("filter seed must be 48 bytes");
            bool nullMode = seed[45] != 0;
            m_b0 = Init (BitConverter.ToUInt64 (seed, 0), nullMode);
            m_b1 = Init (BitConverter.ToUInt64 (seed, 8), nullMode);
            m_split = (long)BitConverter.ToUInt64 (seed, 16);
            m_bulk = seed[44] != 0 ? seed.Skip (24).Take (16).ToArray () : new byte[0];
        }

        static FilterBoundary Init (ulong value, bool nullMode)
        {
            var b = new FilterBoundary ();
            b.Pos0 = (int)((value >> 48) & 0xFFFF);
            b.Pos1 = (int)((value >> 32) & 0xFFFF);
            if (b.Pos0 == b.Pos1)
                b.Pos1 += 1;
            int keyByte = (int)(value & 0xFF);
            b.Byte0 = (byte)((value >> 8) & 0xFF);
            b.Byte1 = (byte)((value >> 16) & 0xFF);
            if (keyByte == 0)
                keyByte = nullMode ? 0 : 0xA5;
            b.Key = (uint)(keyByte * 0x01010101);
            if (nullMode)
            {
                b.Byte0 = 0;
                b.Byte1 = 0;
            }
            return b;
        }

        void ApplyBoundary (byte[] data, FilterBoundary b, long chunkStart, int bufferStart, int size)
        {
            if (size <= 0)
                return;
            for (int i = 0; i < size; ++i)
            {
                int shift = (int)((chunkStart + i) & 3) * 8;
                data[bufferStart + i] ^= (byte)((b.Key >> shift) & 0xFF);
            }
            if (b.Byte0 != 0 && b.Pos0 >= chunkStart && b.Pos0 < chunkStart + size)
                data[bufferStart + (int)(b.Pos0 - chunkStart)] ^= b.Byte0;
            if (b.Byte1 != 0 && b.Pos1 >= chunkStart && b.Pos1 < chunkStart + size)
                data[bufferStart + (int)(b.Pos1 - chunkStart)] ^= b.Byte1;
        }

        public void Apply (byte[] data)
        {
            int size = data.Length;
            if (size == 0)
                return;
            long end = size;

            if (m_bulk.Length > 0)
            {
                long to = Math.Min (end, m_bulk.Length);
                for (long logical = 0; logical < to; ++logical)
                    data[logical] ^= m_bulk[logical];
            }

            if (m_split <= 0)
                ApplyBoundary (data, m_b1, 0, 0, size);
            else if (m_split < end)
            {
                int firstSize = (int)m_split;
                ApplyBoundary (data, m_b0, 0, 0, firstSize);
                ApplyBoundary (data, m_b1, m_split, firstSize, (int)(end - m_split));
            }
            else
                ApplyBoundary (data, m_b0, 0, 0, size);
        }
    }

    // ==================================================================
    // XP3 結構
    // ==================================================================
    class Xp3Segment
    {
        public bool Compressed;
        public long Offset;
        public uint Size;
        public uint PackedSize;
    }

    class Xp3Entry : PackedEntry
    {
        public uint Flags;
        public List<Xp3Segment> Segments = new List<Xp3Segment> ();
        public uint Adler;
        public bool IsProtected;
        public ulong Key;
        public string Hash;
        public int OpenFlag;
    }

    static class Hex
    {
        public static byte[] Decode (string s)
        {
            s = s.Trim ();
            var r = new byte[s.Length / 2];
            for (int i = 0; i < r.Length; ++i)
                r[i] = Convert.ToByte (s.Substring (i * 2, 2), 16);
            return r;
        }

        public static string Encode (byte[] b)
        {
            var sb = new StringBuilder (b.Length * 2);
            foreach (var x in b)
                sb.Append (x.ToString ("x2"));
            return sb.ToString ();
        }
    }

    static class Zlib
    {
        public static byte[] Inflate (byte[] src)
        {
            using (var ms = new MemoryStream (src, 2, src.Length - 2))     // 跳過 zlib 標頭
            using (var ds = new System.IO.Compression.DeflateStream (
                       ms, System.IO.Compression.CompressionMode.Decompress))
            using (var outp = new MemoryStream ())
            {
                var buf = new byte[0x10000];
                int n;
                while ((n = ds.Read (buf, 0, buf.Length)) > 0)
                    outp.Write (buf, 0, n);
                return outp.ToArray ();
            }
        }
    }

    // Hxv4 表是「大端 TJS Variant」
    class TjsBinaryReader
    {
        readonly byte[] m_data;
        int m_pos;

        public TjsBinaryReader (byte[] data) { m_data = data; }

        byte[] Read (int size)
        {
            if (m_pos + size > m_data.Length)
                throw new InvalidDataException ("truncated TJS binary payload");
            var r = new byte[size];
            Buffer.BlockCopy (m_data, m_pos, r, 0, size);
            m_pos += size;
            return r;
        }

        int ReadI32 ()
        {
            var b = Read (4);
            if (BitConverter.IsLittleEndian) Array.Reverse (b);
            return BitConverter.ToInt32 (b, 0);
        }

        long ReadI64 ()
        {
            var b = Read (8);
            if (BitConverter.IsLittleEndian) Array.Reverse (b);
            return BitConverter.ToInt64 (b, 0);
        }

        double ReadF64 ()
        {
            var b = Read (8);
            if (BitConverter.IsLittleEndian) Array.Reverse (b);
            return BitConverter.ToDouble (b, 0);
        }

        string ReadString ()
        {
            int chars = ReadI32 ();
            var b = Read (chars * 2);
            if (BitConverter.IsLittleEndian)
                for (int i = 0; i < b.Length; i += 2) { var t = b[i]; b[i] = b[i + 1]; b[i + 1] = t; }
            return Encoding.Unicode.GetString (b);
        }

        public object ReadValue ()
        {
            int tag = (sbyte)Read (1)[0];
            switch (tag)
            {
            case 0: case 1: return null;
            case 2: return ReadString ();
            case 3: { int n = ReadI32 (); return Read (n); }
            case 4: return ReadI64 ();
            case 5: return ReadF64 ();
            case -127:
                {
                    int n = ReadI32 ();
                    var list = new List<object> (n);
                    for (int i = 0; i < n; ++i) list.Add (ReadValue ());
                    return list;
                }
            case -63:
                {
                    int n = ReadI32 ();
                    var dict = new Dictionary<string, object> ();
                    for (int i = 0; i < n; ++i)
                    {
                        var k = ReadString ();
                        dict[k] = ReadValue ();
                    }
                    return dict;
                }
            default:
                throw new InvalidDataException ("unsupported TJS tag " + tag);
            }
        }
    }

    // ==================================================================
    // GARbro 插件本體
    // ==================================================================
    [Export(typeof (ArchiveFormat))]
    [ExportMetadata ("Priority", 200)]
    public class Hxv4Xp3Opener : ArchiveFormat
    {
        public override string         Tag { get { return "XP3-HXV4"; } }
        public override string Description { get { return "Kirikiri XP3 (Cxdec/Hxv4)"; } }
        public override uint     Signature { get { return 0x0d335058; } }
        public override bool  IsHierarchic { get { return true; } }
        public override bool      CanWrite { get { return false; } }

        static readonly byte[] s_magic = {
            (byte)'X', (byte)'P', (byte)'3', 0x0d, 0x0a, 0x20, 0x0a, 0x1a, 0x8b, 0x67, 0x01 };

        static DripProgram s_drip;
        static Dictionary<string, string> s_names;
        static readonly object s_lock = new object ();
        static bool s_loaded;

        public Hxv4Xp3Opener ()
        {
            Extensions = new[] { "xp3" };
            ContainedFormats = new[] { "TLG", "PNG", "JPEG", "OGG", "WAV", "TXT", "PSB" };
        }

        static string BaseDir
        {
            get { return Path.GetDirectoryName (typeof (Hxv4Xp3Opener).Assembly.Location); }
        }

        // GARbro 根目錄、以及社群慣用的 GameData/ 都找；舊版放在 DLL 同層或 hxv4/ 也還能用。
        static IEnumerable<string> DataCandidates (string dir, string fileName)
        {
            yield return Path.Combine (Path.Combine (dir, "GameData"), fileName);
            yield return Path.Combine (dir, fileName);
            yield return Path.Combine (Path.Combine (dir, "hxv4"), fileName);
        }

        static void EnsureLoaded ()
        {
            if (s_loaded)
                return;
            lock (s_lock)
            {
                if (s_loaded)
                    return;
                var dir = BaseDir;
                foreach (var cand in DataCandidates (dir, "drip_program.json"))
                {
                    if (File.Exists (cand))
                    {
                        try { s_drip = DripProgram.Load (cand); } catch { }
                        break;
                    }
                }
                // 檔名對照表：GARbro 格式 "<64 hex BLAKE2s>:<name>"
                s_names = new Dictionary<string, string> (StringComparer.Ordinal);
                foreach (var cand in DataCandidates (dir, "HxNames.lst"))
                {
                    if (!File.Exists (cand))
                        continue;
                    try
                    {
                        foreach (var line in File.ReadLines (cand, Encoding.UTF8))
                        {
                            var s = line.Trim ();
                            if (s.IndexOf (':') != 64)
                                continue;
                            s_names[s.Substring (0, 64).ToLowerInvariant ()] = s.Substring (65);
                        }
                    }
                    catch { }
                    break;
                }
                s_loaded = true;
            }
        }

        public override ArcFile TryOpen (ArcView file)
        {
            EnsureLoaded ();
            Log ("TryOpen " + file.Name + " drip=" + (s_drip != null) + " names=" + s_names.Count
                 + " base=" + BaseDir);
            if (null == s_drip)
                return null;
            if (!file.View.BytesEqual (0, s_magic))
                return null;

            byte[] blob;
            using (var input = file.CreateStream (0, (uint)Math.Min (file.MaxOffset, uint.MaxValue)))
            using (var ms = new MemoryStream ())
            {
                input.CopyTo (ms);
                blob = ms.ToArray ();
            }

            List<Xp3Entry> entries;
            try
            {
                entries = Parse (blob);
            }
            catch (Exception ex)
            {
                Log ("  parse failed: " + ex.GetType ().Name + " " + ex.Message);
                return null;
            }
            if (null == entries || 0 == entries.Count)
                return null;

            var dir = new List<Entry> (entries.Count);
            foreach (var e in entries)
                dir.Add (e);
            Log ("  ok " + dir.Count + " entries");
            return new ArcFile (file, this, dir);
        }

        // 除錯用：設定環境變數 HXV4_DEBUG=1 才會寫 %TEMP%\hxv4_namefix.log
        static void Log (string msg)
        {
            if (null == Environment.GetEnvironmentVariable ("HXV4_DEBUG"))
                return;
            try
            {
                File.AppendAllText (Path.Combine (Path.GetTempPath (), "hxv4_namefix.log"),
                    DateTime.Now.ToString ("HH:mm:ss ") + msg + "\n");
            }
            catch { }
        }

        public override Stream OpenEntry (ArcFile arc, Entry entry)
        {
            var e = entry as Xp3Entry;
            if (null == e)
                return base.OpenEntry (arc, entry);
            return new MemoryStream (Extract (arc, e), false);
        }

        static byte[] Extract (ArcFile arc, Xp3Entry e)
        {
            var view = arc.File.View;
            var parts = new MemoryStream ();
            foreach (var seg in e.Segments)
            {
                var raw = view.ReadBytes (seg.Offset, seg.PackedSize);
                if (seg.Compressed)
                    raw = Zlib.Inflate (raw);
                parts.Write (raw, 0, raw.Length);
            }
            var data = parts.ToArray ();
            if (e.IsProtected)
            {
                var state = new FilterState (s_drip.BuildFilterState (e.Key, e.OpenFlag & 1));
                state.Apply (data);
            }
            return data;
        }

        // ---------------------------------------------------------- 解析
        static uint U32 (byte[] b, int o) { return BitConverter.ToUInt32 (b, o); }
        static ulong U64 (byte[] b, int o) { return BitConverter.ToUInt64 (b, o); }

        static long ResolveIndexOffset (byte[] blob)
        {
            // 這類包把 index 偏移欄位塞 0x17，真正的 index 在 0x20 的 qword
            long idx = (long)U64 (blob, 11);
            if (idx == 0x17 && blob.Length >= 0x28)
            {
                long cand = (long)U64 (blob, 0x20);
                if (cand > 0 && cand < blob.Length)
                    return cand;
            }
            return idx;
        }

        static byte[] LoadIndex (byte[] blob)
        {
            long offset = ResolveIndexOffset (blob);
            int flag = blob[offset];
            int cursor = (int)offset + 1;
            if (0 == flag)
            {
                long size = (long)U64 (blob, cursor);
                cursor += 8;
                var r = new byte[size];
                Buffer.BlockCopy (blob, cursor, r, 0, (int)size);
                return r;
            }
            if (1 == flag)
            {
                long csize = (long)U64 (blob, cursor);
                cursor += 16;
                var raw = new byte[csize];
                Buffer.BlockCopy (blob, cursor, raw, 0, (int)csize);
                return Zlib.Inflate (raw);
            }
            throw new InvalidDataException ("unsupported XP3 index flag " + flag);
        }

        List<Xp3Entry> Parse (byte[] blob)
        {
            var index = LoadIndex (blob);

            long hxv4Offset = -1;
            int hxv4Size = 0, hxv4Flags = 0;
            int cursor = 0;
            var entries = new List<Xp3Entry> ();

            while (cursor + 12 <= index.Length)
            {
                var tag = Encoding.ASCII.GetString (index, cursor, 4);
                long chunkSize = (long)U64 (index, cursor + 4);
                cursor += 12;
                if (tag == "Hxv4")
                {
                    hxv4Offset = (long)U64 (index, cursor);
                    hxv4Size = (int)U32 (index, cursor + 8);
                    hxv4Flags = BitConverter.ToUInt16 (index, cursor + 12);
                }
                else if (tag == "File")
                {
                    var e = ParseFile (index, cursor, (int)chunkSize);
                    if (null != e)
                        entries.Add (e);
                }
                cursor += (int)chunkSize;
            }
            if (hxv4Offset < 0 || hxv4Size <= 0)
                throw new InvalidDataException ("archive has no Hxv4 table");

            // Hxv4 payload -> zlib -> 大端 TJS Variant
            var payload = new byte[hxv4Size];
            Buffer.BlockCopy (blob, (int)hxv4Offset, payload, 0, hxv4Size);
            var nonce = s_drip.Hxv4Nonces[hxv4Flags & 1];
            var decrypted = XChaCha20Poly1305.Decrypt (s_drip.Hxv4Key, nonce, payload);
            int uncompressed = BitConverter.ToInt32 (decrypted, 0);
            var tableBlob = new byte[decrypted.Length - 4];
            Buffer.BlockCopy (decrypted, 4, tableBlob, 0, tableBlob.Length);
            var raw = Zlib.Inflate (tableBlob);
            if (raw.Length != uncompressed)
                throw new InvalidDataException ("Hxv4 table size mismatch");

            var value = new TjsBinaryReader (raw).ReadValue () as List<object>;
            if (null == value)
                throw new InvalidDataException ("unexpected Hxv4 table shape");

            // 有 startup.tjs 的包，entry index 要 +1
            int entryBase = (entries.Count > 1 && entries[1].Name == "startup.tjs") ? 1 : 0;
            var result = new List<Xp3Entry> ();

            for (int gi = 0; gi + 1 < value.Count; gi += 2)
            {
                var group = value[gi + 1] as List<object>;
                if (null == group)
                    continue;
                for (int ii = 0; ii + 1 < group.Count; ii += 2)
                {
                    var fileHash = group[ii] as byte[];
                    var pair = group[ii + 1] as List<object>;
                    if (null == fileHash || null == pair || pair.Count != 2)
                        continue;

                    long packed = Convert.ToInt64 (pair[0]);
                    ulong key = unchecked((ulong)Convert.ToInt64 (pair[1]));
                    int slot = (int)((packed >> 16) & 0xFFFF);
                    int flag = (int)(packed & 0xFFFF);
                    if (slot != 0)
                        continue;                       // 只處理 slot 0
                    int idx = flag + entryBase;
                    if (idx <= 0 || idx >= entries.Count)
                        continue;                       // idx 0 是警告訊息

                    var e = entries[idx];
                    e.IsProtected = true;
                    e.Key = key;
                    e.Hash = Hex.Encode (fileHash);
                    e.OpenFlag = hxv4Flags;

                    string name;
                    if (s_names.TryGetValue (e.Hash, out name) && name.Length > 0)
                    {
                        e.Name = name;
                        e.Type = FormatCatalog.Instance.GetTypeFromName (name, ContainedFormats);
                    }
                    else
                    {
                        e.Name = "entry_" + idx.ToString ("D5") + GuessExt (blob, e);
                        e.Type = FormatCatalog.Instance.GetTypeFromName (e.Name, ContainedFormats);
                    }
                    result.Add (e);
                }
            }
            result.Sort ((a, b) => string.CompareOrdinal (a.Name, b.Name));
            return result;
        }

        static Xp3Entry ParseFile (byte[] index, int start, int size)
        {
            var e = new Xp3Entry ();
            int p = start, end = start + size;
            bool hasSegs = false;
            while (p + 12 <= end)
            {
                var tag = Encoding.ASCII.GetString (index, p, 4);
                long sub = (long)U64 (index, p + 4);
                p += 12;
                if (sub < 0 || p + sub > index.Length)
                    break;
                int s = (int)sub;
                if (tag == "info")
                {
                    e.Flags = U32 (index, p);
                    int nameLen = BitConverter.ToUInt16 (index, p + 20);
                    if (nameLen > 0 && p + 22 + nameLen * 2 <= index.Length)
                        e.Name = Encoding.Unicode.GetString (index, p + 22, nameLen * 2);
                    e.UnpackedSize = (uint)(long)U64 (index, p + 4);
                    e.Size = (uint)(long)U64 (index, p + 12);
                }
                else if (tag == "segm")
                {
                    int n = s / 28;
                    for (int i = 0; i < n; ++i)
                    {
                        int o = p + i * 28;
                        e.Segments.Add (new Xp3Segment {
                            Compressed = U32 (index, o) != 0,
                            Offset = (long)U64 (index, o + 4),
                            Size = (uint)(long)U64 (index, o + 12),
                            PackedSize = (uint)(long)U64 (index, o + 20),
                        });
                    }
                    hasSegs = n > 0;
                }
                else if (tag == "adlr" && s >= 4)
                {
                    e.Adler = U32 (index, p);
                }
                p += s;
            }
            if (!hasSegs)
                return null;
            e.Offset = e.Segments[0].Offset;
            return e;
        }

        static string GuessExt (byte[] blob, Xp3Entry e)
        {
            try
            {
                var seg = e.Segments[0];
                var head = new byte[16];
                Buffer.BlockCopy (blob, (int)seg.Offset, head, 0, 16);
                if (head[0] == 0x78)
                    return ".bin";
                if (head[0] == 0x89 && head[1] == 0x50) return ".png";
                if (head[0] == 'O' && head[1] == 'g') return ".ogg";
                if (head[0] == 'R' && head[1] == 'I') return ".wav";
                if (head[0] == 0xFF && head[1] == 0xD8) return ".jpg";
                if (head[0] == 'T' && head[1] == 'L') return ".tlg";
                if (head[0] == 'P' && head[1] == 'S') return ".psb";
                if (head[0] == 'B' && head[1] == 'M') return ".bmp";
            }
            catch { }
            return ".bin";
        }
    }
}
