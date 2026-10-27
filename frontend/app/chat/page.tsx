import Link from "next/link";
import ChatPanel from "@/components/ChatPanel";

// the m2 chat agent; ChatPanel is a client component that streams from /api/chat
export default function ChatPage() {
  return (
    <main className="mx-auto w-full max-w-3xl space-y-6 p-4 sm:p-6">
      <header className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Chat</h1>
        <Link href="/dashboard" className="text-sm underline underline-offset-2">
          Dashboard
        </Link>
      </header>
      <ChatPanel />
    </main>
  );
}
