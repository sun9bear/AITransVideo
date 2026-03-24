"use client"

import Link from "next/link"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">工作台</h1>

      <Card>
        <CardHeader>
          <CardTitle>快速开始</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">完成以下四步即可拿到翻译配音视频。</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Step n={1} title="填写 YouTube 链接并创建翻译任务" desc="当前支持公开的 YouTube 视频链接。" />
            <Step n={2} title="按提示完成审核" desc="系统会在关键节点暂停，请确认说话人、翻译和音色。" />
            <Step n={3} title="等待处理完成" desc="配音、对齐和视频合成会自动完成。" />
            <Step n={4} title="在项目详情页下载结果" desc="成品视频、配音音频和字幕文件都可以下载。" />
          </div>
          <div className="flex gap-2">
            <Button><Link href="/translations/new">新建翻译</Link></Button>
            <Button variant="outline"><Link href="/tasks/current">查看当前任务</Link></Button>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader><CardTitle className="text-base">快捷入口</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            <Link href="/voices" className="block rounded-lg border p-3 text-sm hover:bg-muted transition">我的音色 →</Link>
            <Link href="/projects" className="block rounded-lg border p-3 text-sm hover:bg-muted transition">我的项目 →</Link>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function Step({ n, title, desc }: { n: number; title: string; desc: string }) {
  return (
    <div className="rounded-lg border p-3 space-y-1">
      <p className="text-xs text-muted-foreground">第{n}步</p>
      <p className="text-sm font-semibold">{title}</p>
      <p className="text-xs text-muted-foreground">{desc}</p>
    </div>
  )
}
