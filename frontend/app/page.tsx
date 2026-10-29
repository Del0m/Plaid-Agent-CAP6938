import { redirect } from "next/navigation";

// the dashboard is the only page for now
export default function Home() {
  redirect("/dashboard");
}
