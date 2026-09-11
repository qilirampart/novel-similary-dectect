import { Navigate, Route, Routes } from "react-router-dom";

import { ProtectedRoute } from "./ProtectedRoute";
import { AppLayout } from "./layout";
import { DashboardPage } from "./pages/DashboardPage";
import { SingleComparePage } from "./pages/SingleComparePage";
import { BatchTasksPage } from "./pages/BatchTasksPage";
import { ReviewPage } from "./pages/ReviewPage";
import { TaskRecordsPage } from "./pages/TaskRecordsPage";
import { SystemStatusPage } from "./pages/SystemStatusPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { LoginPage } from "./pages/LoginPage";
import { DramaSubtitlePage } from "./pages/DramaSubtitlePage";
import { CoverMonitorPage } from "./pages/CoverMonitorPage";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<AppLayout />}>
          <Route index element={<DashboardPage />} />
          <Route path="/single-compare" element={<SingleComparePage />} />
          <Route path="/batch-tasks" element={<BatchTasksPage />} />
          <Route path="/drama-subtitles" element={<DramaSubtitlePage mode="single" />} />
          <Route path="/drama-subtitles/batch" element={<DramaSubtitlePage mode="batch" />} />
          <Route path="/cover-monitor" element={<CoverMonitorPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/task-records" element={<TaskRecordsPage />} />
          <Route path="/rules" element={<PlaceholderPage kind="rules" />} />
          <Route path="/retrieval-library" element={<PlaceholderPage kind="library" />} />
          <Route path="/logs" element={<PlaceholderPage kind="logs" />} />
          <Route path="/system-status" element={<SystemStatusPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}
