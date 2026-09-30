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
      <Layout.Sider breakpoint="lg" collapsedWidth="0" width={208}>
        {/* 品牌区。原先是裸文本 `Crosslayer`，与菜单项同层级，侧栏"没有头"。
            现在是色块 + 产品名 + 英文副标，并用一条分隔线与菜单分开。 */}
        <div className="cl-brand">
          <span className="cl-brand__mark" aria-hidden />
          <span>
            Crosslayer
            <span className="cl-brand__sub">跨层可观测与诊断</span>
          </span>
        </div>
        <Menu theme="dark" mode="inline" selectedKeys={[selected]} items={MENU} />
      </Layout.Sider>
      <Layout>
        <Layout.Header
          className="cl-header"
          style={{
            background: token.colorBgContainer,
            padding: '0 20px',
            display: 'flex',
            alignItems: 'center',
            gap: 12,
          }}
        >
          <StatusBar />
        </Layout.Header>
        <Layout.Content style={{ padding: 20, maxWidth: 1760, width: '100%', margin: '0 auto' }}>
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
