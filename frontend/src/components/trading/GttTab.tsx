import { Download, Loader2, RefreshCw, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { tradingApi } from '@/api/trading'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn, makeFormatCurrency, sanitizeCSV } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { onModeChange } from '@/stores/themeStore'
import type { GttOrder } from '@/types/trading'
import { showToast } from '@/utils/toast'

const gttStatusColor: Record<string, string> = {
  active: 'bg-blue-500',
  triggered: 'bg-green-500',
  cancelled: 'bg-gray-500',
  expired: 'bg-gray-500',
  rejected: 'bg-red-500',
  disabled: 'bg-amber-500',
  deleted: 'bg-gray-500',
}

function formatPrices(prices: number[], formatCurrency: (n: number) => string): string {
  if (!prices || prices.length === 0) return '-'
  return prices.map((p) => formatCurrency(p)).join(' / ')
}

function formatDateTime(iso?: string): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function GttTab() {
  const { apiKey, user } = useAuthStore()
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])

  const [gtts, setGtts] = useState<GttOrder[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cancellingId, setCancellingId] = useState<string | null>(null)

  const fetchGtts = useCallback(
    async (showRefresh = false) => {
      if (!apiKey) {
        setIsLoading(false)
        return
      }
      if (showRefresh) setIsRefreshing(true)
      try {
        const response = await tradingApi.getGttOrderbook(apiKey)
        if (response.status === 'success') {
          setGtts((response.data as GttOrder[]) ?? [])
          setError(null)
        } else {
          // 501 (broker lacks native GTT, or analyze mode) — treat as empty, show hint
          setGtts([])
          setError(response.message || 'GTT orders are not available')
        }
      } catch (e) {
        const axiosError = e as { response?: { data?: { message?: string }; status?: number } }
        const status = axiosError.response?.status
        const msg =
          axiosError.response?.data?.message ||
          (status === 501
            ? "GTT orders are not supported for this broker yet"
            : 'Failed to fetch GTT orders')
        setGtts([])
        setError(msg)
      } finally {
        setIsLoading(false)
        setIsRefreshing(false)
      }
    },
    [apiKey]
  )

  useEffect(() => {
    fetchGtts()
  }, [fetchGtts])

  useEffect(() => {
    const unsubscribe = onModeChange(() => fetchGtts())
    return () => unsubscribe()
  }, [fetchGtts])

  const handleCancel = async (triggerId: string) => {
    setCancellingId(triggerId)
    try {
      const response = await tradingApi.cancelGttOrder(triggerId)
      if (response.status === 'success') {
        showToast.success(`GTT cancelled: ${triggerId}`, 'orders')
        // Give the broker a moment to reflect the change.
        setTimeout(() => fetchGtts(true), 800)
      } else {
        showToast.error(response.message || 'Failed to cancel GTT', 'orders')
      }
    } catch (e) {
      const axiosError = e as { response?: { data?: { message?: string } } }
      showToast.error(
        axiosError.response?.data?.message || 'Failed to cancel GTT',
        'orders'
      )
    } finally {
      setCancellingId(null)
    }
  }

  const exportToCSV = () => {
    if (gtts.length === 0) {
      showToast.error('No data to export', 'system')
      return
    }
    try {
      const headers = [
        'Trigger ID',
        'Type',
        'Symbol',
        'Exchange',
        'Trigger Prices',
        'Last Price',
        'Legs',
        'Status',
        'Created',
        'Expires',
      ]
      const rows = gtts.map((g) => [
        sanitizeCSV(g.trigger_id),
        sanitizeCSV(g.trigger_type),
        sanitizeCSV(g.symbol),
        sanitizeCSV(g.exchange),
        sanitizeCSV(g.trigger_prices.join(' / ')),
        sanitizeCSV(g.last_price),
        sanitizeCSV(
          g.legs
            .map((l) => `${l.action} ${l.quantity} @ ${l.price} ${l.pricetype}/${l.product}`)
            .join(' | ')
        ),
        sanitizeCSV(g.status),
        sanitizeCSV(g.created_at ?? ''),
        sanitizeCSV(g.expires_at ?? ''),
      ])
      const csv = [headers, ...rows].map((row) => row.join(',')).join('\n')
      const blob = new Blob([csv], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const filename = `gtt_orderbook_${new Date().toISOString().split('T')[0]}.csv`
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
      showToast.success(`Downloaded ${filename}`, 'clipboard')
    } catch {
      showToast.error('Failed to export CSV', 'system')
    }
  }

  const isCancellable = (status: string) => {
    const s = (status || '').toLowerCase()
    return s === 'active' || s === 'disabled'
  }

  return (
    <div className="space-y-4">
      {/* Action row */}
      <div className="flex items-center justify-end gap-2 flex-wrap">
        <Button
          variant="outline"
          size="sm"
          onClick={() => fetchGtts(true)}
          disabled={isRefreshing}
        >
          <RefreshCw className={cn('h-4 w-4 mr-2', isRefreshing && 'animate-spin')} />
          Refresh
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={exportToCSV}
          disabled={gtts.length === 0}
        >
          <Download className="h-4 w-4 mr-2" />
          Export
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-8 w-8 animate-spin" />
            </div>
          ) : error && gtts.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground">{error}</div>
          ) : gtts.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground">No GTT orders</div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[120px]">Trigger ID</TableHead>
                    <TableHead className="w-[80px]">Type</TableHead>
                    <TableHead className="w-[130px]">Symbol</TableHead>
                    <TableHead className="w-[80px]">Exchange</TableHead>
                    <TableHead className="w-[140px] text-right">Trigger Prices</TableHead>
                    <TableHead className="w-[100px] text-right">Last Price</TableHead>
                    <TableHead>Legs</TableHead>
                    <TableHead className="w-[100px]">Status</TableHead>
                    <TableHead className="w-[120px]">Created</TableHead>
                    <TableHead className="w-[120px]">Expires</TableHead>
                    <TableHead className="w-[70px]">Cancel</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {gtts.map((g) => {
                    const statusClass =
                      gttStatusColor[(g.status || '').toLowerCase()] || 'bg-slate-500'
                    return (
                      <TableRow key={g.trigger_id}>
                        <TableCell className="font-mono text-xs">{g.trigger_id}</TableCell>
                        <TableCell>
                          <Badge variant="secondary">
                            {g.trigger_type === 'two-leg' ? 'OCO' : 'Single'}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-medium">{g.symbol}</TableCell>
                        <TableCell>
                          <Badge variant="outline">{g.exchange}</Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {formatPrices(g.trigger_prices, formatCurrency)}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {formatCurrency(g.last_price)}
                        </TableCell>
                        <TableCell className="text-sm">
                          <div className="flex flex-col gap-1">
                            {g.legs.map((leg, i) => (
                              <div
                                key={`${g.trigger_id}-leg-${i}`}
                                className="flex items-center gap-2"
                              >
                                <Badge
                                  variant={
                                    leg.action.toUpperCase() === 'BUY' ? 'default' : 'destructive'
                                  }
                                  className={cn(
                                    'h-5 px-1.5 text-[10px]',
                                    leg.action.toUpperCase() === 'BUY' && 'bg-green-500'
                                  )}
                                >
                                  {leg.action.toUpperCase()}
                                </Badge>
                                <span className="font-mono">
                                  {leg.quantity} @ {formatCurrency(leg.price)}
                                </span>
                                <span className="text-xs text-muted-foreground">
                                  {leg.pricetype} · {leg.product}
                                </span>
                              </div>
                            ))}
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge className={cn('capitalize text-white', statusClass)}>
                            {g.status}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {formatDateTime(g.created_at)}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {formatDateTime(g.expires_at)}
                        </TableCell>
                        <TableCell>
                          {isCancellable(g.status) && (
                            <AlertDialog>
                              <AlertDialogTrigger asChild>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-8 w-8 text-destructive hover:text-destructive hover:bg-destructive/10"
                                  disabled={cancellingId === g.trigger_id}
                                  aria-label={`Cancel GTT ${g.trigger_id}`}
                                >
                                  {cancellingId === g.trigger_id ? (
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                  ) : (
                                    <X className="h-4 w-4" />
                                  )}
                                </Button>
                              </AlertDialogTrigger>
                              <AlertDialogContent>
                                <AlertDialogHeader>
                                  <AlertDialogTitle>Cancel GTT?</AlertDialogTitle>
                                  <AlertDialogDescription>
                                    GTT <span className="font-mono">{g.trigger_id}</span> on{' '}
                                    <span className="font-medium">{g.symbol}</span> will be
                                    removed. This cannot be undone.
                                  </AlertDialogDescription>
                                </AlertDialogHeader>
                                <AlertDialogFooter>
                                  <AlertDialogCancel>Keep</AlertDialogCancel>
                                  <AlertDialogAction
                                    onClick={() => handleCancel(g.trigger_id)}
                                  >
                                    Cancel GTT
                                  </AlertDialogAction>
                                </AlertDialogFooter>
                              </AlertDialogContent>
                            </AlertDialog>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
