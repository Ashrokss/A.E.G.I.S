import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./lib/auth";
import Shell from "./components/Shell";
import Login from "./pages/Login";
import Callback from "./pages/Callback";
import Home from "./pages/Home";
import Runs from "./pages/Runs";
import Inbox from "./pages/Inbox";
import Knowledge from "./pages/Knowledge";
import Accounts from "./pages/Accounts";
import Integrations from "./pages/Integrations";
import Admin from "./pages/Admin";
import Connect from "./pages/Connect";

export default function App() {
  const { identity, ready } = useAuth();

  if (!ready) return null;

  return (
    <Routes>
      <Route path="/auth/callback" element={<Callback />} />
      {!identity ? (
        <Route path="*" element={<Login />} />
      ) : (
        <>
          {/* A new sign-up's first screen: no app shell behind it. */}
          <Route path="/connect" element={<Connect />} />
          <Route element={<Shell />}>
          <Route path="/home" element={<Home />} />
          <Route path="/runs/:runId?" element={<Runs />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/integrations" element={<Integrations />} />
          {identity.role === "admin" && <Route path="/admin" element={<Admin />} />}
          <Route path="*" element={<Navigate to="/home" replace />} />
          </Route>
        </>
      )}
    </Routes>
  );
}
