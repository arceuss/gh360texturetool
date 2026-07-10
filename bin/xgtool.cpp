// xgtool: tile/untile a full GH3 Xbox-360 DXT texture (all mip levels, incl. the
// packed mip tail) using the real XGraphics library from the Xbox 360 SDK.
//
//   xgtool untile <w> <h> <mips> <dxt1|dxt5> <in_record> <out_linearchain>
//   xgtool tile   <w> <h> <mips> <dxt1|dxt5> <in_linearchain> <in_baserecord> <out_record>
//
// linearchain = concatenated linear DXT mip levels, top first, each ceil-block packed.
#include <windows.h>
#include <d3d9.h>
#include <xgraphics.h>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <string>

static UINT alignu(UINT v, UINT a) { return (v + a - 1) & ~(a - 1); }
static UINT blocksW(UINT w) { UINT b = (w + 3) / 4; return b ? b : 1; }
static UINT blocksH(UINT h) { UINT b = (h + 3) / 4; return b ? b : 1; }
// tiled storage of one level (128-texel aligned tile), in bytes
static UINT tileStore(UINT w, UINT h, UINT bpb) {
    return (alignu(w, 128) / 4) * (alignu(h, 128) / 4) * bpb;
}
static UINT linSize(UINT w, UINT h, UINT bpb) { return blocksW(w) * blocksH(h) * bpb; }
// Xbox 360 stores DXT with byte-swapped 16-bit words; swap so we deal in LE DXT.
static void bswap16(unsigned char* p, size_t n) {
    for (size_t i = 0; i + 1 < n; i += 2) { unsigned char t = p[i]; p[i] = p[i+1]; p[i+1] = t; }
}

static std::vector<unsigned char> readFile(const char* p) {
    FILE* f = fopen(p, "rb"); if (!f) { fprintf(stderr, "open fail %s\n", p); exit(2); }
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    std::vector<unsigned char> v(n); fread(v.data(), 1, n, f); fclose(f); return v;
}
static void writeFile(const char* p, const unsigned char* d, size_t n) {
    FILE* f = fopen(p, "wb"); fwrite(d, 1, n, f); fclose(f);
}

int main(int argc, char** argv) {
    if (argc < 7) { fprintf(stderr, "usage error\n"); return 2; }
    std::string mode = argv[1];
    UINT W = atoi(argv[2]), H = atoi(argv[3]), MIPS = atoi(argv[4]);
    bool dxt5 = (std::string(argv[5]) == "dxt5");
    UINT bpb = dxt5 ? 16 : 8;
    D3DFORMAT d3dfmt = dxt5 ? D3DFMT_DXT5 : D3DFMT_DXT1;
    DWORD gpufmt = dxt5 ? GPUTEXTUREFORMAT_DXT4_5 : GPUTEXTUREFORMAT_DXT1;

    // Per-level absolute byte offset within the record, straight from XGraphics.
    // The base and mip data are separate allocations; GH3 stores them contiguously
    // (record > base tile) or shares one tile (record <= base tile, small textures),
    // so pick the mip-region base accordingly.
    IDirect3DTexture9 tex;
    UINT baseSize = 0, mipSize = 0;
    XGSetTextureHeader(W, H, MIPS, 0, d3dfmt, D3DPOOL_DEFAULT, 0, 0, 0, &tex, &baseSize, &mipSize);

    if (mode == "untile") {
        std::vector<unsigned char> rec = readFile(argv[6]);
        UINT mipOff = (rec.size() <= baseSize) ? 0 : baseSize;
        std::vector<unsigned char> out;
        for (UINT L = 0; L < MIPS; ++L) {
            UINT lw = W >> L ? W >> L : 1, lh = H >> L ? H >> L : 1;
            UINT rp = blocksW(lw) * bpb, ls = linSize(lw, lh, bpb);
            UINT o = (L == 0 ? 0u : mipOff) + XGGetMipLevelOffset(&tex, 0, L);
            std::vector<unsigned char> lin(ls, 0);
            if (o < rec.size())
                XGUntileTextureLevel(W, H, L, gpufmt, 0, lin.data(), rp, NULL,
                                     rec.data() + o, NULL);
            bswap16(lin.data(), lin.size());   // 360 -> LE DXT
            out.insert(out.end(), lin.begin(), lin.end());
        }
        writeFile(argv[7], out.data(), out.size());
        return 0;
    } else if (mode == "tile") {
        if (argc < 8) { fprintf(stderr, "tile needs base record\n"); return 2; }
        std::vector<unsigned char> lin = readFile(argv[6]);
        std::vector<unsigned char> rec = readFile(argv[7]);   // base to preserve padding
        UINT mipOff = (rec.size() <= baseSize) ? 0 : baseSize;
        bswap16(lin.data(), lin.size());   // LE DXT -> 360 word order
        UINT linCur = 0;
        for (UINT L = 0; L < MIPS; ++L) {
            UINT lw = W >> L ? W >> L : 1, lh = H >> L ? H >> L : 1;
            UINT rp = blocksW(lw) * bpb, ls = linSize(lw, lh, bpb);
            if (linCur + ls > lin.size()) break;
            UINT o = (L == 0 ? 0u : mipOff) + XGGetMipLevelOffset(&tex, 0, L);
            if (o < rec.size())
                XGTileTextureLevel(W, H, L, gpufmt, 0, rec.data() + o, NULL,
                                   lin.data() + linCur, rp, NULL);
            linCur += ls;
        }
        writeFile(argv[8], rec.data(), rec.size());
        return 0;
    }
    else if (mode == "untile1") {
        // untile1 <w> <h> <level> <dxt> <in_rec> <byteoffset> <out_lin>
        UINT L = MIPS;  // reuse arg4 as level
        UINT boff = (UINT)strtoul(argv[7], NULL, 0);
        std::vector<unsigned char> rec = readFile(argv[6]);
        UINT lw = W >> L ? W >> L : 1, lh = H >> L ? H >> L : 1;
        UINT rp = blocksW(lw) * bpb, ls = linSize(lw, lh, bpb);
        std::vector<unsigned char> lin(ls, 0);
        XGUntileTextureLevel(W, H, L, gpufmt, 0, lin.data(), rp, NULL,
                             rec.data() + boff, NULL);
        bswap16(lin.data(), lin.size());
        writeFile(argv[8], lin.data(), lin.size());
        return 0;
    }
    fprintf(stderr, "bad mode\n"); return 2;
}
