// 把 ComfyUI 生产图里那条多模态序列，在 llama.cpp 上一发解完，逐 token 导出两份 hidden state：
//   post.bin —— 过了最终 RMSNorm 的（llama.cpp 默认出口 t_embd）
//   pre.bin  —— 没过的那一份（DiT 实际要的那类），靠 src/models/qwen3vl.cpp 的本地补丁 + llama-ext.h 的 layer_inp 口
//
// 三个不显然的点（今天实测出来的，写成注释免得下次再撞）：
//  1) logits_last 必须给 false。qwen3vl.cpp:126 在最后一层按 inp_out_ids 裁剪，只有标了 logits 的行留下来 ——
//     这就是 HTTP 那条口只回"最后一个文本块 102 行"的原因。给每个 token 都标 logits 才能拿到整条。
//  2) n_batch 要 ≥ 单个 chunk 的 token 数（人像那 chunk 是 1728），否则一个 chunk 拆成多次 llama_decode，
//     而 embeddings 缓冲只保留最后一次 decode 的内容 —— 所以这里逐 chunk 收割，不等全部跑完。
//  3) 媒体标记来自 mtmd_context_params.media_marker（本机这版默认 "<__media__>"）；
//     prompt 里标记数必须等于 bitmap 数，否则 mtmd_tokenize 返回 1。
//
// 编译：换机器直接跑 `tools/llama_gate/build.sh`（打补丁 + 编库 + 编这个二进制，一条命令）。
// 下面是它内部执行的那条 g++（库在 llama.cpp 的 build/bin，ROCm 版）：
//   cd ~/work/llama/llama.cpp && g++ -O1 -std=c++17 \
//     -I include -I ggml/include -I tools/mtmd -I src \
//     ~/work/qwen-image/tools/llama_gate/dump_hidden.cpp \
//     -L build/bin -lllama -lmtmd -Wl,-rpath,$PWD/build/bin \
//     -o ~/work/qwen-image/tools/llama_gate/bin/dump_hidden
//   产物落 repo 里而不是 /tmp：调用方是 ComfyUI 的节点，机器重启后还得能用（`bin/` 已 gitignore，
//   它是编译产物不是源）。换机器/换 ROCm 都要重编一遍，命令就抄这一行。
//   （库必须排在源文件**之后**：先 -l 后 .cpp 会被 bfd 直接丢掉，报一堆 undefined reference to llama_free）
//
// 这份是 §19 那版的续：**多导出每个文本段的 token id**（在 meta.json 的 chunks[].ids 里）。
// 用它的下游是 `tools/llama_gate/keep_rows.py` —— 那份是 `comfy/text_encoders/qwen_image21.py:53-66`
// 那套"丢 system turn + 丢视觉 token"规则在 Python 侧的对等移植，锚点从 token id 现算，不写死 14。

#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"
#include "llama-ext.h"   // llama_set/get_embeddings_layer_inp 在这（staging 头，符号确实导出：nm 见 _Z30llama_..._layer_inp...）

#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

static std::string read_file(const char * path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { std::fprintf(stderr, "读不到 %s\n", path); std::exit(2); }
    return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

static void write_bin(const std::string & path, const std::vector<float> & v) {
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char *>(v.data()), static_cast<std::streamsize>(v.size() * sizeof(float)));
    std::printf("写出 %s：%zu 个 float\n", path.c_str(), v.size());
}

int main(int argc, char ** argv) {
    const char * model_path = nullptr;
    const char * mmproj_path = nullptr;
    const char * prompt_path = nullptr;
    const char * out_dir = "/tmp/q3vl-gate";
    std::vector<std::string> imgs;
    bool want_post = true;   // --no-post：DiT 要的是 pre-norm 那份，post 只在研究时对照用（一次 45 MB）

    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "-m") && i + 1 < argc)            model_path = argv[++i];
        else if (!std::strcmp(argv[i], "--mmproj") && i + 1 < argc) mmproj_path = argv[++i];
        else if (!std::strcmp(argv[i], "--prompt") && i + 1 < argc) prompt_path = argv[++i];
        else if (!std::strcmp(argv[i], "--img") && i + 1 < argc)    imgs.emplace_back(argv[++i]);
        else if (!std::strcmp(argv[i], "--out") && i + 1 < argc)    out_dir = argv[++i];
        else if (!std::strcmp(argv[i], "--no-post"))               want_post = false;
    }
    if (!model_path || !mmproj_path || !prompt_path || imgs.empty()) {
        std::printf("用法：dump_hidden -m <gguf> --mmproj <mmproj> --prompt <带媒体标记的文本> --img <png> [...] [--out 目录] [--no-post]\n");
        return 2;
    }

    // 自己打时间戳：7.5 秒的总墙钟里"载模型"和"跑 prefill"必须拆开，否则收益判断是猜的
    const auto t_wall0 = std::chrono::steady_clock::now();
    auto ms_since = [&t_wall0](std::chrono::steady_clock::time_point t0) {
        return (long long)std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t0).count();
    };
    auto t_mark = t_wall0;
    auto lap = [&](const char * what) {
        const long long ms = ms_since(t_mark);
        t_mark = std::chrono::steady_clock::now();
        std::printf("[计时] %-22s %lld ms（累计 %lld ms）\n", what, ms, ms_since(t_wall0));
        std::fflush(stdout);
    };

    llama_backend_init();

    llama_model_params mparams = llama_model_default_params();
    mparams.n_gpu_layers = -1;  // 全放 GPU
    llama_model * model = llama_model_load_from_file(model_path, mparams);
    if (!model) { std::fprintf(stderr, "模型载不动\n"); return 1; }

    mtmd_context_params cparams = mtmd_context_params_default();
    cparams.use_gpu = true;
    cparams.print_timings = false;
    cparams.media_marker = "<__media__>";   // 与 --prompt 文件里写的标记一致
    mtmd_context * mctx = mtmd_init_from_file(mmproj_path, model, cparams);
    if (!mctx) { std::fprintf(stderr, "mmproj 载不动\n"); return 1; }
    lap("模型载入");
    std::printf("媒体标记 = %s\n", cparams.media_marker);

    lap("mmproj 初始化");
    const int32_t n_layer = llama_model_n_layer(model);
    const int32_t n_embd = llama_model_n_embd_out(model);
    std::printf("n_layer=%d n_embd_out=%d\n", n_layer, n_embd);

    llama_context_params lparams = llama_context_default_params();
    lparams.n_ctx = 8192;
    lparams.n_batch = 4096;     // ≥ 单 chunk 最大 token 数
    lparams.n_ubatch = 4096;
    lparams.pooling_type = LLAMA_POOLING_TYPE_NONE;
    lparams.embeddings = true;
    // 这一版没有 logits_all 这个字段了：每 token 要不要输出，由 batch 里的 logits 标记决定，
    // 也就是下面 mtmd_helper_eval_chunk_single(..., logits_last = false, ...) 那一路。
    llama_context * ctx = llama_init_from_model(model, lparams);
    if (!ctx) { std::fprintf(stderr, "context 建不起来\n"); return 1; }
    lap("context 建立");
    llama_set_embeddings(ctx, true);
    llama_set_embeddings_layer_inp(ctx, static_cast<uint32_t>(n_layer), true);  // ← 补丁开的那道口

    std::string text = read_file(prompt_path);
    mtmd_input_text itext;
    itext.text = text.c_str();
    itext.text_len = text.size();
    itext.add_special = false;
    itext.parse_special = true;

    std::vector<const mtmd_bitmap *> bitmaps;
    std::vector<mtmd_helper_bitmap_wrapper> wraps;
    for (auto & p : imgs) {
        wraps.push_back(mtmd_helper_bitmap_init_from_file(mctx, p.c_str(), false, mtmd_helper_init_opt_default()));
        if (!wraps.back().bitmap) { std::fprintf(stderr, "图解码失败：%s\n", p.c_str()); return 1; }
        bitmaps.push_back(wraps.back().bitmap);
    }

    mtmd_input_chunks * chunks = mtmd_input_chunks_init();
    int32_t rc = mtmd_tokenize(mctx, chunks, &itext, bitmaps.data(), bitmaps.size());
    if (rc) { std::fprintf(stderr, "mtmd_tokenize 返回 %d（标记数要与图数相等）\n", rc); return 1; }
    lap("读图 + tokenize");
    std::printf("chunk 数=%zu 总 token=%zu 总 pos=%lld\n", mtmd_input_chunks_size(chunks),
                mtmd_helper_get_n_tokens(chunks), static_cast<long long>(mtmd_helper_get_n_pos(chunks)));

    std::vector<float> pre_all, post_all;
    std::string meta = "{\"n_embd\":" + std::to_string(n_embd) + ",\"chunks\":[";
    llama_pos n_past = 0;
    const size_t n_chunks = mtmd_input_chunks_size(chunks);
    for (size_t i = 0; i < n_chunks; ++i) {
        const mtmd_input_chunk * chunk = mtmd_input_chunks_get(chunks, i);
        const enum mtmd_input_chunk_type type = mtmd_input_chunk_get_type(chunk);
        size_t n_tokens = 0;
        // 为什么要 ids：下游要照 `comfy/text_encoders/qwen_image21.py:53-66` 算"丢掉 system turn"，
        // 那条规则的锚点是**第二个 im_start 的行位置**（本机这颗分词恰好是 14）。§19 的判读脚本把 14 写死了，
        // 那是研究代码；在线这一档必须从 token id 现算 —— 换一颗模型或换一句含特殊标记的指令，写死的就会错位，
        // 而且**错位的条件是向量、不是崩溃**，看不出来（§18 那个作废的 0.687 就是这么来的）。
        const llama_token * ids = nullptr;
        if (type == MTMD_INPUT_CHUNK_TYPE_TEXT) {
            ids = mtmd_input_chunk_get_tokens_text(chunk, &n_tokens);
        } else {
            n_tokens = mtmd_input_chunk_get_n_tokens(chunk);
        }
        llama_pos new_past = n_past;
        int32_t r = mtmd_helper_eval_chunk_single(mctx, ctx, chunk, n_past, 0 /*seq_id*/,
                                                  lparams.n_batch, false /*logits_last*/, &new_past);
        if (r) { std::fprintf(stderr, "chunk %zu 解算失败 rc=%d\n", i, r); return 1; }
        lap("  eval 这段");

        const float * pre = llama_get_embeddings_layer_inp(ctx, static_cast<uint32_t>(n_layer));
        const float * post = want_post ? llama_get_embeddings_ith(ctx, 0) : nullptr;
        if (!pre || (want_post && !post)) {
            std::fprintf(stderr, "拿不到向量（post=%p pre=%p）—— pre 为空说明补丁没生效或 layer_inp 没请求\n",
                         (const void *)post, (const void *)pre);
            return 1;
        }
        for (size_t t = 0; t < n_tokens; ++t) {
            for (int32_t e = 0; e < n_embd; ++e) {
                pre_all.push_back(pre[t * n_embd + e]);
                if (want_post) post_all.push_back(post[t * n_embd + e]);
            }
        }
        if (i) meta += ",";
        meta += std::string("{\"kind\":") + (type == MTMD_INPUT_CHUNK_TYPE_TEXT ? "\"text\"" : "\"image\"") +
                ",\"n_tokens\":" + std::to_string(n_tokens) +
                ",\"start_pos\":" + std::to_string(static_cast<long long>(n_past));
        if (ids) {  // 只有文本段有 id；图像段的行区间由 n_tokens 累加得到，与 image_spans 等价
            meta += ",\"ids\":[";
            for (size_t t = 0; t < n_tokens; ++t) {
                if (t) meta += ",";
                meta += std::to_string(static_cast<long long>(ids[t]));
            }
            meta += "]";
        }
        meta += "}";
        std::printf("chunk %zu：%s %zu token（pos %lld..%lld）\n", i,
                    type == MTMD_INPUT_CHUNK_TYPE_TEXT ? "text " : "image", n_tokens,
                    static_cast<long long>(n_past), static_cast<long long>(new_past));
        n_past = new_past;
    }
    lap("解码全部");
    // total_rows 一律从 pre_all 算：两份的行数本来该相等，但 --no-post 时 post_all 是空的
    meta += "],\"total_rows\":" + std::to_string(pre_all.size() / static_cast<size_t>(n_embd)) +
            ",\"补丁取的层\":" + std::to_string(n_layer) + "}";

    const std::string dir(out_dir);
    if (want_post) write_bin(dir + "/post.bin", post_all);
    write_bin(dir + "/pre.bin", pre_all);
    std::ofstream mf(dir + "/meta.json");
    mf << meta;
    std::printf("meta：%s\n", meta.c_str());

    mtmd_input_chunks_free(chunks);
    for (auto & w : wraps) {
        if (w.bitmap) mtmd_bitmap_free(w.bitmap);
    }
    mtmd_free(mctx);
    llama_free(ctx);
    llama_model_free(model);
    llama_backend_free();
    return 0;
}
