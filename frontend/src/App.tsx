import { Layout, Menu, theme } from 'antd'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { GpuDataNotice } from './components/GpuDataNotice'
import { HealthBanner } from './components/HealthBanner'
import { StatusBar } from './components/StatusBar'
import TopologyPage from './pages/TopologyPage'
import WorkloadsPage from './pages/WorkloadsPage'
import EventsPage from './pages/EventsPage'
import AlertsPage from './pages/AlertsPage'
import DiagnosesPage from './pages/DiagnosesPage'

const MENU = [
  { key: '/topology', label: <Link to="/topology">拓扑</Link> },
  { key: '/workloads', label: <Link to="/workloads">工作负载</Link> },
  { key: '/events', label: <Link to="/events">事件</Link> },
  { key: '/alerts', label: <Link to="/alerts">告警</Link> },
  { key: '/diagnoses', label: <Link to="/diagnoses">诊断</Link> },
]

export default function App() {
  const location = useLocation()
  const { token } = theme.useToken()
  const selected = MENU.find((m) => location.pathname.startsWith(m.key))?.key ?? '/topology'

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Sider breakpoint="lg" collapsedWidth="0">
        <div style={{ color: '#fff', padding: 16, fontWeight: 600, fontSize: 16 }}>
          Crosslayer
        </div>
        <Menu theme="dark" mode="inline" selectedKeys={[selected]} items={MENU} />
      </Layout.Sider>
      <Layout>
        <Layout.Header
          style={{
            background: token.colorBgContainer,
            padding: '0 16px',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
          }}
        >
          <StatusBar />
        </Layout.Header>
        <Layout.Content style={{ margin: 16 }}>
          <HealthBanner />
          <GpuDataNotice />
          <Routes>
            <Route path="/" element={<Navigate to="/topology" replace />} />
            <Route path="/topology" element={<TopologyPage />} />
            <Route path="/workloads" element={<WorkloadsPage />} />
            <Route path="/events" element={<EventsPage />} />
            <Route path="/alerts" element={<AlertsPage />} />
            <Route path="/diagnoses" element={<DiagnosesPage />} />
          </Routes>
        </Layout.Content>
      </Layout>
    </Layout>
  )
}
