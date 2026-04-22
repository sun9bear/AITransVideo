import {
  CheckCircle2,
  Download,
  FileVideo2,
  FolderArchive,
  Languages,
  ListVideo,
  Mic2,
  SlidersHorizontal,
  Sparkles,
  WandSparkles,
} from "lucide-react"
import { DIGITAL_DELIVERABLES } from "./company-info"

const TASK_FLOW = [
  "支持 YouTube 链接和视频上传两种入口，适合真实业务输入。",
  "快捷版适合自动流程，工作台版适合人工复核和精调。",
  "可选转录方案、说话人数与处理模式，不是模板表单占位页。",
]

const RESULT_FLOW = [
  "任务完成后保留项目列表、到期时间、处理状态和历史记录。",
  "每个项目都能下载配音视频、配音音频和素材包。",
  "这类结果页能直接证明网站提供的是可交付的数字化服务。",
]

export function ProductProof() {
  return (
    <section className="bg-background py-20 sm:py-24">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="text-xs font-semibold uppercase tracking-wider text-primary">
            真实产品证明
          </p>
          <h2 className="mt-3 text-3xl font-bold tracking-tight text-foreground sm:text-4xl">
            不是模板站，而是可创建任务并下载结果的工作台
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            AIVideoTrans 当前公开销售的是数字化视频本地化服务。用户在站内创建翻译任务、查看项目状态、下载配音结果与素材包，
            不依赖人工发货或单页演示图。
          </p>
        </div>

        <div className="mt-12 grid gap-8 lg:grid-cols-2">
          <article className="rounded-3xl border border-border bg-card p-6 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wider text-primary">
                  真实界面 01
                </p>
                <h3 className="mt-2 text-xl font-semibold text-foreground">
                  新建翻译任务
                </h3>
              </div>
              <span className="rounded-full border border-primary/20 bg-primary/8 px-3 py-1 text-xs font-medium text-primary">
                任务创建页
              </span>
            </div>

            <div className="mt-5 overflow-hidden rounded-2xl border border-white/10 bg-slate-950 text-slate-100 shadow-[0_20px_80px_-36px_rgba(15,23,42,0.85)]">
              <div className="flex items-center justify-between border-b border-white/10 px-4 py-3 text-sm text-slate-300">
                <div className="flex items-center gap-2">
                  <span className="h-2.5 w-2.5 rounded-full bg-red-400/90" />
                  <span className="h-2.5 w-2.5 rounded-full bg-amber-300/90" />
                  <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/90" />
                </div>
                <span>新建翻译</span>
                <span className="text-xs text-slate-500">AIVideoTrans</span>
              </div>

              <div className="space-y-4 p-4">
                <div className="rounded-2xl border border-white/8 bg-white/[0.03] p-4">
                  <div className="flex gap-3 text-sm">
                    <span className="rounded-xl bg-sky-500 px-4 py-2 font-medium text-white">
                      YouTube 链接
                    </span>
                    <span className="rounded-xl border border-white/10 px-4 py-2 text-slate-300">
                      上传视频
                    </span>
                  </div>
                  <div className="mt-4 rounded-2xl border border-white/10 bg-slate-900 px-4 py-3 text-slate-500">
                    https://www.youtube.com/watch?v=...
                  </div>
                  <p className="mt-3 text-xs leading-6 text-slate-400">
                    仅用于翻译您本人或已获授权的视频内容；创建前可选择处理方案、音色与说话人配置。
                  </p>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-2xl border border-sky-400/60 bg-sky-500/10 p-4 shadow-[inset_0_0_0_1px_rgba(56,189,248,0.2)]">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-lg font-semibold">快捷版</span>
                      <span className="rounded-full bg-emerald-500/15 px-3 py-1 text-xs font-semibold text-emerald-300">
                        Express
                      </span>
                    </div>
                    <p className="mt-3 text-sm leading-6 text-slate-300">
                      全自动流程，完成转录、翻译、配音与初步对齐，适合快速出片。
                    </p>
                  </div>

                  <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-lg font-semibold">工作台版</span>
                      <span className="rounded-full bg-sky-500/15 px-3 py-1 text-xs font-semibold text-sky-300">
                        Studio
                      </span>
                    </div>
                    <p className="mt-3 text-sm leading-6 text-slate-300">
                      支持审校译文、克隆原声音色和逐段微调，更适合稳定商业输出。
                    </p>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3">
                    <div className="text-xs text-slate-400">转录方案</div>
                    <div className="mt-2 flex items-center gap-2 text-sm">
                      <Mic2 className="h-4 w-4 text-sky-300" />
                      AssemblyAI（音频上传）
                    </div>
                  </div>
                  <div className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3">
                    <div className="text-xs text-slate-400">说话人数</div>
                    <div className="mt-2 flex items-center gap-2 text-sm">
                      <SlidersHorizontal className="h-4 w-4 text-sky-300" />
                      自动
                    </div>
                  </div>
                </div>

                <div className="inline-flex rounded-2xl bg-sky-600 px-5 py-3 text-sm font-semibold text-white shadow-lg shadow-sky-900/30">
                  创建任务
                </div>
              </div>
            </div>

            <ul className="mt-5 space-y-3 text-sm text-muted-foreground">
              {TASK_FLOW.map((item) => (
                <li key={item} className="flex items-start gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </article>

          <article className="rounded-3xl border border-border bg-card p-6 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wider text-primary">
                  真实界面 02
                </p>
                <h3 className="mt-2 text-xl font-semibold text-foreground">
                  项目结果与下载交付物
                </h3>
              </div>
              <span className="rounded-full border border-primary/20 bg-primary/8 px-3 py-1 text-xs font-medium text-primary">
                结果列表页
              </span>
            </div>

            <div className="mt-5 overflow-hidden rounded-2xl border border-white/10 bg-slate-950 text-slate-100 shadow-[0_20px_80px_-36px_rgba(15,23,42,0.85)]">
              <div className="flex items-center justify-between border-b border-white/10 px-4 py-3 text-sm text-slate-300">
                <span>视频翻译</span>
                <span className="rounded-xl bg-sky-600 px-3 py-1 text-xs font-medium text-white">
                  新建翻译
                </span>
              </div>

              <div className="space-y-4 p-4">
                <div className="rounded-2xl border border-amber-400/25 bg-amber-400/8 px-4 py-3 text-sm text-amber-100">
                  每个项目最长保留 7 天，过期后自动删除，请及时下载结果文件。
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  {[1, 2, 3, 4].map((card) => (
                    <div
                      key={card}
                      className="rounded-2xl border border-white/10 bg-white/[0.03] p-4"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-white">
                            Job{" "}
                            {card === 1
                              ? "Bed88548..."
                              : card === 2
                                ? "28854cd7..."
                                : card === 3
                                  ? "B2429298..."
                                  : "319547d9..."}
                          </div>
                          <div className="mt-2 text-xs text-slate-400">
                            可查看项目状态、历史记录与到期时间
                          </div>
                        </div>
                        <span className="rounded-full bg-emerald-500/15 px-3 py-1 text-xs font-semibold text-emerald-300">
                          已完成
                        </span>
                      </div>

                      <div className="mt-4 rounded-2xl bg-slate-900/90 p-4">
                        <div className="flex h-28 items-center justify-center rounded-xl border border-dashed border-white/10 bg-white/[0.03] text-slate-400">
                          <ListVideo className="h-7 w-7" />
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <span className="rounded-xl border border-white/10 px-3 py-2 text-xs">
                            配音视频
                          </span>
                          <span className="rounded-xl border border-white/10 px-3 py-2 text-xs">
                            配音音频
                          </span>
                          <span className="rounded-xl border border-emerald-400/30 px-3 py-2 text-xs text-emerald-300">
                            素材包下载
                          </span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            <ul className="mt-5 space-y-3 text-sm text-muted-foreground">
              {RESULT_FLOW.map((item) => (
                <li key={item} className="flex items-start gap-2">
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </article>
        </div>

        <div className="mt-10 rounded-3xl border border-border bg-muted/40 p-6 sm:p-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div className="max-w-2xl">
              <p className="text-xs font-semibold uppercase tracking-wider text-primary">
                购买内容
              </p>
              <h3 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">
                购买的是数字化视频本地化服务和真实交付物
              </h3>
              <p className="mt-3 zh-body text-muted-foreground">
                付费套餐解锁的是账户内处理能力和工作台权益，不是单次静态下载页。任务完成后，用户可在站内查看项目状态、复核内容并下载交付结果。
              </p>
            </div>
            <div className="rounded-2xl border border-primary/20 bg-primary/8 px-4 py-3 text-sm text-primary">
              支持长视频、多说话人、增量重生成和剪映草稿导出
            </div>
          </div>

          <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {DIGITAL_DELIVERABLES.map((item) => {
              const icon =
                item === "配音视频" ? (
                  <FileVideo2 className="h-4 w-4" />
                ) : item === "配音音频" ? (
                  <Mic2 className="h-4 w-4" />
                ) : item === "字幕/素材包" ? (
                  <FolderArchive className="h-4 w-4" />
                ) : item === "剪映草稿工程" ? (
                  <Download className="h-4 w-4" />
                ) : item === "人工复核工作台" ? (
                  <Languages className="h-4 w-4" />
                ) : item === "增量重生成能力" ? (
                  <WandSparkles className="h-4 w-4" />
                ) : (
                  <Sparkles className="h-4 w-4" />
                )

              return (
                <div
                  key={item}
                  className="flex items-center gap-3 rounded-2xl border border-border bg-card px-4 py-3 text-sm text-foreground shadow-sm"
                >
                  <span className="rounded-full bg-primary/10 p-2 text-primary">
                    {icon}
                  </span>
                  <span>{item}</span>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </section>
  )
}
