import { Alert, App as AntApp, Button, ConfigProvider, Input, Select, Spin, Tag } from 'antd'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import zhCN from 'antd/locale/zh_CN'
import { ApiError, explainError, fetchApi } from './api'
import type { CreateRequest, Decision, DecisionRequest, MessageRequest, Snapshot } from './api'
import type { components } from './api.generated'
import { useConversation } from './useConversation'
import 'antd/dist/reset.css'
import './styles.css'

type Outcome = components['schemas']['OutcomeView']
type Proposal = components['schemas']['ProposalView']
type Conversation = components['schemas']['ConversationView']
type Message = components['schemas']['MessageView']

const states: Record<Conversation['page_state'], string> = {
  investigating: '调查中', waiting_decision: '等待你的决定', executing: '执行中',
  verifying: '验证中', finished: '已结束', interrupted: '调查已中断',
  needs_verification: '处置结果待核实',
}

const proposalStates: Record<Proposal['status'], string> = {
  pending_approval: '待审批', approved: '已批准', rejected: '已拒绝', denied: '规则拒绝',
  expired: '授权已过期', executing: '执行中', succeeded: '已完成',
  failed: '执行未通过', unknown: '待核实',
}

const notExecuted: Record<NonNullable<Outcome['not_executed_reason']>, string> = {
  expired: '授权已过期', drift: '目标已被改动', revision_changed: '历史版本已变化',
  probe_ambiguous: '首页探针未返回唯一序列', baseline_unavailable: '无法取得首页探针基线',
  api_rejected: '集群拒绝请求或写入前检查失败',
}

const verificationReasons: Record<NonNullable<Outcome['verification_reason']>, string> = {
  probe_ambiguous: '首页探针未返回唯一序列', interrupted: '处置后验证中断',
  template_mismatch: '集群回读的 Pod 模板与提案不一致',
}

const verificationLabels = {
  improved: '改善', no_improvement: '无改善', worsened: '恶化', unknown: '未知',
}

const hypothesisLabels = { pending: '待核实', supported: '有证据支持', refuted: '有证据反驳', inconclusive: '证据不足' }
const relationLabels = { supports: '支持', refutes: '反驳' }
const observationAnchor = (hypothesisId: string, observationId: string) => `${hypothesisId}--${observationId}`

function Section({ title, children, action }: { title: string, children: ReactNode, action?: ReactNode }) {
  return <section className="panel"><header className="section-head"><h2>{title}</h2>{action}</header>{children}</section>
}

function useSubmission() {
  const [busy, setBusy] = useState(false)
  const pending = useRef<{ key: string, id: string } | undefined>(undefined)
  const sending = useRef(false)
  const [error, setError] = useState('')
  const { message } = AntApp.useApp()

  async function submit(key: string, send: (id: string) => Promise<unknown>, after: () => Promise<void>, success?: string) {
    if (sending.current) return
    sending.current = true
    setBusy(true)
    setError('')
    const requestId = pending.current?.key === key ? pending.current.id : crypto.randomUUID()
    pending.current = { key, id: requestId }
    try {
      await send(requestId)
      pending.current = undefined
      await after()
      if (success) void message.success(success)
    } catch (error) {
      setError(explainError(error))
      if (error instanceof ApiError) {
        pending.current = undefined
        if (['hash_mismatch', 'not_waiting', 'not_pending', 'not_found', 'conversation_active'].includes(error.code)) {
          void after()
        }
      }
    } finally {
      sending.current = false
      setBusy(false)
    }
  }

  return { submit, busy, error }
}

function NewInvestigation({ refresh, cancel }: { refresh: () => Promise<void>, cancel?: () => void }) {
  const [services, setServices] = useState<string[]>([])
  const [service, setService] = useState<string>()
  const [symptoms, setSymptoms] = useState('')
  const [unavailable, setUnavailable] = useState(false)
  const [loading, setLoading] = useState(true)
  const [version, setVersion] = useState(0)
  const { busy, error, submit } = useSubmission()

  useEffect(() => {
    let current = true
    fetchApi<{ services: string[] }>('/services').then(({ services }) => {
      if (!current) return
      setServices(services)
      setService(services.includes('frontend') ? 'frontend' : services[0])
    }).catch(() => { if (current) setUnavailable(true) })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [version])

  function start(event: FormEvent) {
    event.preventDefault()
    if (!service || !symptoms.trim()) return
    void submit(JSON.stringify([service, symptoms.trim()]), (request_id) => {
      const body: CreateRequest = { request_id, service, symptoms: symptoms.trim() }
      return fetchApi('/conversations', body)
    }, refresh)
  }

  return <section className="start-panel">
    <div className="start-title"><h1>发起故障调查</h1><p>选择受影响的服务，描述你观察到的现象。</p></div>
    <form className="start-form" onSubmit={start}>
      {unavailable && <Alert type="error" showIcon message="暂时无法读取服务列表。" action={
        <Button size="small" onClick={() => { setUnavailable(false); setLoading(true); setVersion(n => n + 1) }}>重试</Button>
      } />}
      {error && <Alert type="error" showIcon message={error} />}
      <label htmlFor="service">受影响的服务</label>
      <Select id="service" value={service} onChange={setService} loading={loading} disabled={loading || busy}
        options={services.map(value => ({ value, label: value }))} placeholder="选择服务" showSearch />
      <label htmlFor="symptoms">故障现象</label>
      <Input.TextArea id="symptoms" rows={5} value={symptoms} maxLength={8000} showCount
        placeholder="例如：首页从今天上午开始返回 500，商品页仍能打开。"
        onChange={event => setSymptoms(event.target.value)} />
      <div className="actions">
        {cancel && <Button onClick={cancel}>返回当前调查</Button>}
        <Button htmlType="submit" type="primary" loading={busy} disabled={!service || !symptoms.trim()}>发起调查</Button>
      </div>
    </form>
  </section>
}

function deliveryLabel(message: Message) {
  if (message.delivery === 'delivered') return `已送入调查（第 ${message.delivered_round} 轮）`
  if (message.delivery === 'unconfirmed') return '未确认送入'
  return message.delivery === 'received' ? '已收到' : ''
}

function Dialogue({ snapshot, conversation, refresh }: { snapshot: Snapshot, conversation: Conversation, refresh: () => Promise<void> }) {
  const [hint, setHint] = useState('')
  const { busy, error, submit } = useSubmission()

  function send(event: FormEvent) {
    event.preventDefault()
    if (!hint.trim()) return
    const content = hint.trim()
    void submit(content, async (request_id) => {
      const body: MessageRequest = { request_id, content }
      await fetchApi(`/conversations/${encodeURIComponent(conversation.id)}/messages`, body)
      setHint('')
    }, refresh)
  }

  return <Section title="对话">
    <div className="messages">
      {snapshot.messages?.map(message => <article className={`message ${message.role}`} key={message.id}>
        <div className="message-meta"><strong>{message.role === 'user' ? '你' : 'Vigil'}</strong>
          {message.delivery && <span>{deliveryLabel(message)}</span>}
        </div>
        <p>{message.content}</p>
      </article>)}
    </div>
    {!conversation.finished && <form onSubmit={send} className="hint-form">
      {error && <Alert type="error" showIcon message={error} />}
      <label htmlFor="hint">补充线索</label>
      <Input.TextArea id="hint" rows={2} value={hint} maxLength={8000}
        placeholder="例如：订单页正常，这个变化在最近一次发布后出现。"
        onChange={event => setHint(event.target.value)} />
      <Button type="primary" htmlType="submit" loading={busy} disabled={!hint.trim()}>发送线索</Button>
    </form>}
  </Section>
}

function DecisionCard({ proposal, evidence, refresh }: { proposal: Proposal, evidence: Snapshot['evidence'], refresh: () => Promise<void> }) {
  // 卡片指纹覆盖假设陈述与证据关系，这里必须展示同一份内容；锚点带假设 ID，同一观测挂在多个假设下时不会跳错。
  const basis = evidence?.find(item => item.hypothesis_id === proposal.hypothesis_id)
  const { error, busy, submit } = useSubmission()
  const [reason, setReason] = useState('')
  const hash = proposal.card_hash

  function makeDecision(decision: Decision) {
    if (!hash) return
    void submit(JSON.stringify([proposal.id, decision, hash, reason]), (request_id) => {
      const body: DecisionRequest = { request_id, decision, card_hash: hash, reason }
      return fetchApi(`/proposals/${encodeURIComponent(proposal.id)}/decisions`, body)
    }, refresh, '决定已记录')
  }

  return <Section title="回滚提案" action={<Tag>{proposalStates[proposal.status]}</Tag>}>
    <p className="target">{proposal.target}</p>
    {proposal.rollback && <>
      <div className="revision"><span>版本 {proposal.rollback.from_revision}</span><span aria-hidden="true">→</span><strong>版本 {proposal.rollback.to_revision}</strong></div>
      <div className="table-wrap"><table>
        <caption>Pod 模板变更</caption>
        <thead><tr><th>字段</th><th>当前值</th><th>目标值</th></tr></thead>
        <tbody>{proposal.rollback.changes.map(change => <tr key={change.path}>
          <th scope="row"><code>{change.path}</code></th>
          <td>{change.sensitive ? '已隐藏' : change.from ?? '无'}</td>
          <td>{change.sensitive ? '已变化' : change.to ?? '将被删除'}</td>
        </tr>)}</tbody>
      </table></div>
    </>}
    <dl className="details">
      {basis && <div><dt>关联假设</dt><dd>{basis.statement}</dd></div>}
      <div><dt>依据</dt><dd>{proposal.reason}</dd></div>
      {proposal.evidence_ids.length > 0 && <div><dt>关联证据</dt><dd>{proposal.evidence_ids.map(id => {
        const obs = basis?.observations.find(item => item.id === id)
        const label = obs?.relation ? relationLabels[obs.relation] : '观测'
        return <a className="evidence-link" href={basis ? `#${observationAnchor(basis.hypothesis_id, id)}` : undefined} key={id}>{label} · {id}</a>
      })}</dd></div>}
      {proposal.prediction && <div><dt>预期结果</dt><dd>{proposal.prediction}</dd></div>}
      <div><dt>风险说明</dt><dd><ul>{proposal.risk.map((reason, index) => <li key={index}>{reason}</li>)}</ul></dd></div>
      <div><dt>验证方式</dt><dd>{proposal.verification}</dd></div>
    </dl>
    {error && <Alert type="error" showIcon message={error} />}
    {proposal.status === 'pending_approval' && hash && <div className="decision-form">
      <label htmlFor="decision-reason">决定说明（选填）</label>
      <Input id="decision-reason" value={reason} maxLength={1000} onChange={e => setReason(e.target.value)} />
      <div className="actions">
        <Button disabled={busy} onClick={() => makeDecision('rejected')}>拒绝</Button>
        <Button type="primary" loading={busy} onClick={() => makeDecision('approved')}>批准回滚</Button>
      </div>
    </div>}
  </Section>
}

function Execution({ snapshot }: { snapshot: Snapshot }) {
  const outcome = snapshot.outcome
  return <Section title="执行与验证">
    {outcome && <dl className="details">
      <div><dt>集群写入</dt><dd>{outcome.send_state === 'sent' ? '已写入' : outcome.send_state === 'not_sent' ? '未写入' : '无法确认'}</dd></div>
      {outcome.not_executed_reason && <div><dt>未执行</dt><dd>{notExecuted[outcome.not_executed_reason]}</dd></div>}
      {outcome.verification && <div><dt>首页探针</dt><dd>{verificationLabels[outcome.verification]}</dd></div>}
      {outcome.verification_reason && <div><dt>验证说明</dt><dd>{verificationReasons[outcome.verification_reason]}</dd></div>}
    </dl>}
    {snapshot.followups?.map(followup => <Alert key={followup.id} className="followup"
      type={followup.status === 'open' ? 'warning' : 'success'} showIcon
      message={followup.status === 'open' ? '人工待办 · 待核实' : '人工待办 · 已解决'}
      description={<><p>{followup.reason}</p>{followup.resolution && <p>{followup.resolution}</p>}
        <small>{followup.proposal_id} · {followup.id}</small></>} />)}
  </Section>
}

function Vigil() {
  const { snapshot, connection, refresh } = useConversation()
  const [newForm, setNewForm] = useState(false)
  const previousId = useRef<string | undefined>(undefined)
  const conversation = snapshot?.conversation

  useEffect(() => {
    if (previousId.current !== conversation?.id) {
      previousId.current = conversation?.id
      setNewForm(false)
    }
  }, [conversation?.id])

  return <>
    <header className="site-header"><div className="shell site-row">
      <span className="brand"><span aria-hidden="true" className="mark">V</span><strong>Vigil</strong></span>
      <span className={`connection ${connection}`} role="status"><i />
        {connection === 'connected' ? '已连接' : connection === 'reconnecting' ? '重连中' : '正在连接'}
      </span>
    </div></header>
    <main className="shell">
      {!snapshot && <div className="loading"><Spin /><p>{connection === 'reconnecting' ? '等待连接恢复' : '正在读取当前调查'}</p></div>}
      {snapshot && (!conversation || newForm) && <NewInvestigation refresh={refresh} cancel={conversation ? () => setNewForm(false) : undefined} />}
      {snapshot && conversation && !newForm && <>
        <div className="workspace-head"><div><h1>故障调查</h1><p>{conversation.service} <span>·</span> {conversation.investigation_id}</p></div>
          {conversation.finished && <Button onClick={() => setNewForm(true)}>开始新调查</Button>}
        </div>
        <Dialogue key={conversation.id} snapshot={snapshot} conversation={conversation} refresh={refresh} />
        <Section title="调查进展" action={<Tag color={conversation.page_state === 'needs_verification' ? 'orange' : 'blue'}>{states[conversation.page_state]}</Tag>}>
          {snapshot.progress?.running_tool && <p className="running"><Spin size="small" />正在查询 {snapshot.progress.running_tool.name}</p>}
          {snapshot.progress?.supported_hypotheses.length ? <ul className="hypotheses">{snapshot.progress.supported_hypotheses.map(item => <li key={item.id}>{item.statement}</li>)}</ul>
            : <p className="muted">暂时没有已获证据支持的假设。</p>}
        </Section>
        <Section title="证据">
          {!snapshot.evidence?.length && <p className="muted">暂时没有观测记录。</p>}
          {snapshot.evidence?.map(item => <article className="hypothesis" key={item.hypothesis_id}>
            <h3>{item.statement}</h3><Tag>{hypothesisLabels[item.status]}</Tag>
            {item.observations.map(obs => <div className="observation" id={observationAnchor(item.hypothesis_id, obs.id)} key={obs.id}>
              <small>{obs.relation ? relationLabels[obs.relation] : obs.status === 'empty' ? '无结果' : obs.status === 'error' ? '查询失败' : '观测'}</small>
              <p>{obs.summary}</p>
            </div>)}
          </article>)}
        </Section>
        {snapshot.proposal && <DecisionCard key={snapshot.proposal.id} proposal={snapshot.proposal} evidence={snapshot.evidence} refresh={refresh} />}
        {(snapshot.outcome || !!snapshot.followups?.length) && <Execution snapshot={snapshot} />}
      </>}
    </main>
  </>
}

createRoot(document.getElementById('root')!).render(
  <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#185c56', borderRadius: 8, fontFamily: 'Inter, "Noto Sans CJK SC", system-ui, sans-serif' } }}>
    <AntApp><Vigil /></AntApp>
  </ConfigProvider>,
)
