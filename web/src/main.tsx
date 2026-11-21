import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router";
import { SessionRoute, Workspace } from "./workspace";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("网页入口缺失");

createRoot(root).render(
  <BrowserRouter>
    <Routes>
      <Route element={<Workspace />}>
        <Route index element={<SessionRoute />} />
        <Route path="sessions/:sessionId" element={<SessionRoute />} />
      </Route>
    </Routes>
  </BrowserRouter>,
);
