// 取数服务：ComfyUI 第 3 档（llama.cpp 出编码）的常驻后端。
//
// 为什么是常驻的：原先每发都"起进程 → 映射 8.9 GB 模型 → 建 context → 算 → 写 45 MB 盘 → 退出"，
// 光载模型就 1.23 秒、建 context 0.21 秒（§25.1 分段计时）。用量是几百次/天，常驻一次载入就全省了。
//
// 三个不显然的点（dump_hidden.cpp 那版实测出来的，别改回去）：
//  1) logits_last 必须给 false。qwen3vl.cpp:126 在最后一层按 inp_out_ids 裁剪，只有标了 logits 的行留下来。
//  2) n_batch 要 ≥ 单个 chunk 的 token 数（人像那 chunk 是 1728），否则 embeddings 缓冲只保留最后一次
//     decode 的内容 —— 所以逐 chunk 收割，不等全部跑完。
//  3) 媒体标记来自 mtmd_context_params.media_marker（本机默认 "<__media__>"）；
//     prompt 里标记数必须等于 bitmap 数，否则 mtmd_tokenize 返回 1。
//
// 帧格式（全部小端）见 tools/llama_gate/protocol.py 的头注释；两端独立实现，靠端到端对拍对齐。
//
// 编译：tools/llama_gate/build.sh（打补丁 + 编库 + 编这个二进制）。
// 起停：systemd（tools/llama_gate/llama-gate.service）。

#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"
#include "llama-ext.h"

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// ---------------------------------------------------------------- 配置

static std::string env_str(const char * key, const std::string & fallback) {
    const char * v = std::getenv(key);
    return (v && *v) ? std::string(v) : fallback;
}

static int env_int(const char * key, int fallback) {
    const char * v = std::getenv(key);
    return (v && *v) ? std::atoi(v) : fallback;
}

// ---------------------------------------------------------------- 帧

static const char MAGIC[4] = {'L', 'G', 'A', 'T'};
static const uint8_t VERSION = 1;
static const uint8_t FLAG_DEBUG = 1;

static bool read_exact(int fd, void * buf, size_t n) {
    auto * p = static_cast<uint8_t *>(buf);
    size_t got = 0;
    while (got < n) {
        const ssize_t r = ::recv(fd, p + got, n - got, 0);
        if (r <= 0) return false;
        got += static_cast<size_t>(r);
    }
    return true;
}

static bool write_exact(int fd, const void * buf, size_t n) {
    const auto * p = static_cast<const uint8_t *>(buf);
    size_t put = 0;
    while (put < n) {
        const ssize_t r = ::send(fd, p + put, n - put, MSG_NOSIGNAL);
        if (r <= 0) return false;
        put += static_cast<size_t>(r);
    }
    return true;
}

// 出错时也要把**原因**带回去（帧格式：magic | status | msg_len(4) | msg），
// 客户端只看 status 不够 —— 现场要的是"媒体标记数要与图数相等"这种话。
static void write_error(int fd, const std::string & msg) {
    const uint8_t head[5] = {'L', 'G', 'A', 'T', 1};
    const uint32_t len = static_cast<uint32_t>(msg.size());
    uint8_t lenb[4];
    std::memcpy(lenb, &len, 4);
    (void) write_exact(fd, head, sizeof(head));
    (void) write_exact(fd, lenb, sizeof(lenb));
    (void) write_exact(fd, msg.data(), msg.size());
    std::fprintf(stderr, "取数服务：%s\n", msg.c_str());
}

// ---------------------------------------------------------------- 切行

static const llama_token IM_START = 151644;   // qwen 词表里的 im_start；上游那条规则也拿它当锚点

struct Kept {
    std::vector<uint32_t> rows;
    std::vector<uint32_t> slots;
};

// 照 `tools/llama_gate/keep_rows.py` 的 kept_row_index 逐条移植：丢 system turn（第二个 im_start 之前）+
// 丢两段视觉 token，锚点从 token id 现算。**错位不会崩，只会静默画错**（§18 那个作废的 0.687），
// 所以这儿不写死任何行号。
static Kept kept_rows(const std::vector<std::vector<llama_token>> & text_ids,
                      const std::vector<size_t> & chunk_tokens,
                      const std::vector<bool> & chunk_is_text) {
    std::vector<llama_token> tokens;               // 文本是裸 id，图像是一个占位符 -1
    std::vector<std::pair<size_t, size_t>> spans;  // 图像段的 (起始行, 行数)
    size_t cursor = 0;
    for (size_t i = 0; i < chunk_tokens.size(); ++i) {
        if (chunk_is_text[i]) {
            tokens.insert(tokens.end(), text_ids[i].begin(), text_ids[i].end());
        } else {
            tokens.push_back(-1);
            spans.emplace_back(cursor, chunk_tokens[i]);
        }
        cursor += chunk_tokens[i];
    }

    std::vector<size_t> im_starts;
    size_t offset = 0, si = 0;
    for (size_t i = 0; i < tokens.size(); ++i) {
        if (tokens[i] == -1) offset += spans[si++].second - 1;   // 一个占位符代表 size 行
        else if (tokens[i] == IM_START) im_starts.push_back(i + offset);
    }

    std::vector<bool> keep(cursor, true);
    const size_t drop_head = (im_starts.size() > 1) ? im_starts[1] : 0;
    for (size_t r = 0; r < drop_head; ++r) keep[r] = false;

    Kept out;
    for (auto & sp : spans) {
        for (size_t r = sp.first; r < sp.first + sp.second; ++r) keep[r] = false;
        size_t before = 0;
        for (size_t r = 0; r < sp.first; ++r) if (keep[r]) ++before;
        out.slots.push_back(static_cast<uint32_t>(before));
    }
    for (size_t r = 0; r < keep.size(); ++r) if (keep[r]) out.rows.push_back(static_cast<uint32_t>(r));
    return out;
}

int main() {
    const std::string gguf = env_str("LLAMA_GATE_GGUF", "");
    const std::string mmproj = env_str("LLAMA_GATE_MMPROJ", "");
    const std::string sock_path = env_str("LLAMA_GATE_SOCK", "/run/llama-gate/gate.sock");
    const int n_ctx = env_int("LLAMA_GATE_N_CTX", 8192);
    const int n_batch = env_int("LLAMA_GATE_N_BATCH", 4096);
    const int n_gpu_layers = env_int("LLAMA_GATE_N_GPU_LAYERS", -1);
    const std::string media_marker = env_str("LLAMA_GATE_MEDIA_MARKER", "<__media__>");
    if (gguf.empty() || mmproj.empty()) {
        std::fprintf(stderr, "要设 LLAMA_GATE_GGUF 与 LLAMA_GATE_MMPROJ（模板见 tools/llama_gate/llama-gate.env）\n");
        return 2;
    }

    const auto t0 = std::chrono::steady_clock::now();
    auto ms_since = [&t0](std::chrono::steady_clock::time_point t) {
        return (long long)std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t).count();
    };

    llama_backend_init();
    llama_model_params mparams = llama_model_default_params();
    mparams.n_gpu_layers = n_gpu_layers;
    llama_model * model = llama_model_load_from_file(gguf.c_str(), mparams);
    if (!model) { std::fprintf(stderr, "模型载不动：%s\n", gguf.c_str()); return 1; }

    mtmd_context_params cparams = mtmd_context_params_default();
    cparams.use_gpu = true;
    cparams.print_timings = false;
    cparams.media_marker = media_marker.c_str();
    mtmd_context * mctx = mtmd_init_from_file(mmproj.c_str(), model, cparams);
    if (!mctx) { std::fprintf(stderr, "mmproj 载不动：%s\n", mmproj.c_str()); return 1; }
    std::printf("[计时] 模型 + mmproj 载入 %lld ms\n", ms_since(t0));
    std::fflush(stdout);

    const int32_t n_layer = llama_model_n_layer(model);
    const int32_t n_embd = llama_model_n_embd_out(model);
    std::printf("n_layer=%d n_embd_out=%d n_ctx=%d n_batch=%d 标记=%s\n",
                n_layer, n_embd, n_ctx, n_batch, media_marker.c_str());
    std::fflush(stdout);

    // --- socket ---
    ::unlink(sock_path.c_str());   // 重启时旧的还在，不删 bind 会失败（Review Focus 3）
    const int listen_fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd < 0) { std::fprintf(stderr, "socket 建不起来：%s\n", std::strerror(errno)); return 1; }
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    if (sock_path.size() >= sizeof(addr.sun_path)) {
        std::fprintf(stderr, "socket 路径太长：%s\n", sock_path.c_str());
        return 1;
    }
    std::memcpy(addr.sun_path, sock_path.c_str(), sock_path.size() + 1);
    if (::bind(listen_fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
        std::fprintf(stderr, "bind %s 失败：%s\n", sock_path.c_str(), std::strerror(errno));
        return 1;
    }
    ::chmod(sock_path.c_str(), 0666);   // 让同机的其他用户（比如 ComfyUI 那个）也能连
    if (::listen(listen_fd, 8) < 0) { std::fprintf(stderr, "listen 失败：%s\n", std::strerror(errno)); return 1; }
    std::printf("监听 %s\n", sock_path.c_str());
    std::fflush(stdout);

    // --- 循环 ---
    for (;;) {
        const int fd = ::accept(listen_fd, nullptr, nullptr);
        if (fd < 0) {
            if (errno == EINTR) continue;
            std::fprintf(stderr, "accept 失败：%s\n", std::strerror(errno));
            continue;
        }

        uint8_t header[7];
        if (!read_exact(fd, header, sizeof(header))) { ::close(fd); continue; }
        if (std::memcmp(header, MAGIC, 4) || header[4] != VERSION) {
            std::fprintf(stderr, "请求帧头不对（magic/version），断开\n");
            ::close(fd);
            continue;
        }
        const uint8_t flags = header[5];
        const uint8_t n_imgs = header[6];

        std::vector<std::vector<uint8_t>> imgs(n_imgs);
        bool bad = false;
        for (uint8_t i = 0; i < n_imgs; ++i) {
            uint8_t lenb[4];
            if (!read_exact(fd, lenb, 4)) { bad = true; break; }
            const uint32_t len = uint32_t(lenb[0]) | (uint32_t(lenb[1]) << 8) |
                                 (uint32_t(lenb[2]) << 16) | (uint32_t(lenb[3]) << 24);
            imgs[i].resize(len);
            if (len && !read_exact(fd, imgs[i].data(), len)) { bad = true; break; }
        }
        std::string prompt;
        if (!bad) {
            uint8_t lenb[4];
            if (!read_exact(fd, lenb, 4)) bad = true;
            else {
                const uint32_t len = uint32_t(lenb[0]) | (uint32_t(lenb[1]) << 8) |
                                     (uint32_t(lenb[2]) << 16) | (uint32_t(lenb[3]) << 24);
                prompt.resize(len);
                if (len && !read_exact(fd, &prompt[0], len)) bad = true;
            }
        }
        if (bad) { ::close(fd); continue; }

        // --- 解算 ---
        mtmd_input_text itext;
        itext.text = prompt.c_str();
        itext.text_len = prompt.size();
        itext.add_special = false;
        itext.parse_special = true;

        std::vector<mtmd_helper_bitmap_wrapper> wraps;
        std::vector<const mtmd_bitmap *> bitmaps;
        for (auto & buf : imgs) {
            wraps.push_back(mtmd_helper_bitmap_init_from_buf(
                mctx, buf.data(), buf.size(), false, mtmd_helper_init_opt_default()));
            if (!wraps.back().bitmap) {
                write_error(fd, "第 " + std::to_string(wraps.size()) + " 张图解码失败");
                ::close(fd);
                wraps.pop_back();
                bad = true;
                break;
            }
            bitmaps.push_back(wraps.back().bitmap);
        }
        if (bad) continue;

        mtmd_input_chunks * chunks = mtmd_input_chunks_init();
        const int32_t rc = mtmd_tokenize(mctx, chunks, &itext, bitmaps.data(), bitmaps.size());
        if (rc) {
            write_error(fd, "mtmd_tokenize 返回 " + std::to_string(rc) +
                             "（prompt 里的媒体标记数要与图数相等）");
            mtmd_input_chunks_free(chunks);
            for (auto & w : wraps) if (w.bitmap) mtmd_bitmap_free(w.bitmap);
            ::close(fd);
            continue;
        }

        llama_context_params lparams = llama_context_default_params();
        lparams.n_ctx = n_ctx;
        lparams.n_batch = n_batch;
        lparams.n_ubatch = n_batch;
        lparams.pooling_type = LLAMA_POOLING_TYPE_NONE;
        lparams.embeddings = true;
        llama_context * ctx = llama_init_from_model(model, lparams);
        if (!ctx) {
            write_error(fd, "context 建不起来（n_ctx/n_batch 或显存不够）");
            mtmd_input_chunks_free(chunks);
            for (auto & w : wraps) if (w.bitmap) mtmd_bitmap_free(w.bitmap);
            ::close(fd);
            continue;
        }
        llama_set_embeddings(ctx, true);
        llama_set_embeddings_layer_inp(ctx, static_cast<uint32_t>(n_layer), true);

        std::vector<float> pre_all;
        std::vector<std::vector<llama_token>> text_ids;   // 每个文本段的 token id（切行要用）
        std::vector<size_t> chunk_tokens;
        std::vector<bool> chunk_is_text;
        std::string meta = "{\"n_embd\":" + std::to_string(n_embd) + ",\"chunks\":[";
        llama_pos n_past = 0;
        bool eval_failed = false;
        std::string eval_err;
        const size_t n_chunks = mtmd_input_chunks_size(chunks);
        for (size_t i = 0; i < n_chunks; ++i) {
            const mtmd_input_chunk * chunk = mtmd_input_chunks_get(chunks, i);
            const enum mtmd_input_chunk_type type = mtmd_input_chunk_get_type(chunk);
            size_t n_tokens = 0;
            const llama_token * ids = nullptr;
            if (type == MTMD_INPUT_CHUNK_TYPE_TEXT) {
                ids = mtmd_input_chunk_get_tokens_text(chunk, &n_tokens);
            } else {
                n_tokens = mtmd_input_chunk_get_n_tokens(chunk);
            }
            // n_batch 守卫：embeddings 缓冲只有 n_batch 行，chunk 超过它会被拆成多次 decode，
            // 只留最后一次的内容（越界读，而且**静默出错**）。换模型时这个值最容易被忘掉。
            if (n_tokens > static_cast<size_t>(n_batch)) {
                eval_failed = true;
                eval_err = "第 " + std::to_string(i) + " 段有 " + std::to_string(n_tokens) +
                           " 个 token，超过 LLAMA_GATE_N_BATCH=" + std::to_string(n_batch) +
                           "（embeddings 缓冲只留最后一次 decode 的内容，会静默出错）";
                break;
            }
            llama_pos new_past = n_past;
            const int32_t r = mtmd_helper_eval_chunk_single(
                mctx, ctx, chunk, n_past, 0, n_batch, false, &new_past);
            if (r) {
                eval_failed = true;
                eval_err = "第 " + std::to_string(i) + " 段解算失败（rc=" + std::to_string(r) + "）";
                break;
            }
            const float * pre = llama_get_embeddings_layer_inp(ctx, static_cast<uint32_t>(n_layer));
            if (!pre) {
                eval_failed = true;
                eval_err = "拿不到 pre 向量 —— 补丁没生效或 layer_inp 没请求";
                break;
            }
            for (size_t t = 0; t < n_tokens; ++t) {
                for (int32_t e = 0; e < n_embd; ++e) pre_all.push_back(pre[t * n_embd + e]);
            }
            chunk_is_text.push_back(ids != nullptr);
            if (ids) {
                text_ids.emplace_back(ids, ids + n_tokens);
            } else {
                text_ids.emplace_back();
            }
            chunk_tokens.push_back(n_tokens);
            // meta：debug 帧要带上它，离线判读靠它重建 pre.bin 的坐标（ids 是切行的锚点）
            if (i) meta += ",";
            meta += std::string("{\"kind\":") + (ids ? "\"text\"" : "\"image\"") +
                    ",\"n_tokens\":" + std::to_string(n_tokens) +
                    ",\"start_pos\":" + std::to_string(static_cast<long long>(n_past));
            if (ids) {
                meta += ",\"ids\":[";
                for (size_t t = 0; t < n_tokens; ++t) {
                    if (t) meta += ",";
                    meta += std::to_string(static_cast<long long>(ids[t]));
                }
                meta += "]";
            }
            meta += "}";
            n_past = new_past;
        }
        llama_free(ctx);
        // eval 中途失败要**报错**，不能把已算出的前几段当成完整序列回出去（那是静默画错）
        if (eval_failed) {
            write_error(fd, eval_err);
            mtmd_input_chunks_free(chunks);
            for (auto & w : wraps) if (w.bitmap) mtmd_bitmap_free(w.bitmap);
            ::close(fd);
            continue;
        }
        const size_t total_rows = pre_all.size() / static_cast<size_t>(n_embd);
        meta += "],\"total_rows\":" + std::to_string(total_rows) +
                ",\"补丁取的层\":" + std::to_string(n_layer) + "}";

        // --- 响应 ---
        const uint32_t embd32 = static_cast<uint32_t>(n_embd);
        uint8_t out[14] = {'L', 'G', 'A', 'T', 0};
        std::memcpy(out + 9, &embd32, 4);

        if (flags & FLAG_DEBUG) {
            // debug：回**全序列** + meta（离线判读用，§19/§23/§24 那套）
            const uint32_t rows32 = static_cast<uint32_t>(total_rows);
            std::memcpy(out + 5, &rows32, 4);
            out[13] = 0;
            (void) write_exact(fd, out, sizeof(out));
            const uint32_t meta_len = static_cast<uint32_t>(meta.size());
            uint8_t ml[4];
            std::memcpy(ml, &meta_len, 4);
            (void) write_exact(fd, ml, 4);
            (void) write_exact(fd, meta.data(), meta.size());
            if (total_rows) (void) write_exact(fd, pre_all.data(), pre_all.size() * sizeof(float));
        } else {
            // --- 切行 ---
            const Kept kept = kept_rows(text_ids, chunk_tokens, chunk_is_text);
            if (kept.rows.empty() || kept.rows.size() == total_rows) {
                write_error(fd, "切行结果不对（切完 " + std::to_string(kept.rows.size()) + " 行 / 共 " +
                                 std::to_string(total_rows) + " 行）—— 分段或标记算错了，别把这条序列喂给 DiT");
                mtmd_input_chunks_free(chunks);
                for (auto & w : wraps) if (w.bitmap) mtmd_bitmap_free(w.bitmap);
                ::close(fd);
                continue;
            }
            std::vector<float> out_rows;
            out_rows.reserve(kept.rows.size() * static_cast<size_t>(n_embd));
            for (const uint32_t r : kept.rows) {
                const float * p = pre_all.data() + static_cast<size_t>(r) * n_embd;
                out_rows.insert(out_rows.end(), p, p + n_embd);
            }
            const uint32_t rows32 = static_cast<uint32_t>(kept.rows.size());
            std::memcpy(out + 5, &rows32, 4);
            out[13] = static_cast<uint8_t>(kept.slots.size());
            (void) write_exact(fd, out, sizeof(out));
            for (const uint32_t s : kept.slots) {
                uint8_t sb[4];
                std::memcpy(sb, &s, 4);
                (void) write_exact(fd, sb, 4);
            }
            if (!out_rows.empty()) (void) write_exact(fd, out_rows.data(), out_rows.size() * sizeof(float));
            std::printf("解完：%zu chunk、全序列 %zu 行 → 留 %zu 行（slots %zu 个）\n",
                        n_chunks, total_rows, kept.rows.size(), kept.slots.size());
        }

        mtmd_input_chunks_free(chunks);
        for (auto & w : wraps) if (w.bitmap) mtmd_bitmap_free(w.bitmap);
        ::close(fd);
        std::fflush(stdout);
    }
}
