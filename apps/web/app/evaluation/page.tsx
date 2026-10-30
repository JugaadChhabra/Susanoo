import type { Metadata } from "next";
import { EvaluationConsole } from "@/components/evaluation/console";
import "./platform.css";

export const metadata: Metadata = {
  title: "Susanoo | Evaluation Workspace",
  robots: { index: false, follow: false },
};

export default function EvaluationPage() {
  return <EvaluationConsole />;
}
