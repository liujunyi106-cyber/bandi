import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "@/App";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { KernelGate } from "@/components/layout/KernelGate";
import "@/index.css";

const container = document.getElementById("root");
if (!container) throw new Error("root container not found in index.html");

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <ErrorBoundary>
        <KernelGate>
          <App />
        </KernelGate>
      </ErrorBoundary>
    </BrowserRouter>
  </StrictMode>,
);
