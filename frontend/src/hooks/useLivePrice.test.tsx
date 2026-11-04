import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@/test/test-utils'
import { useLivePrice } from './useLivePrice'

/**
 * The closed-position branch.
 *
 * `useLivePrice` freezes an item's REST values when its quantity is zero, so a
 * closed position's realized P&L percentage does not drift as the market moves.
 * `quantity` is optional on `PriceableItem`, and `item.quantity || 0` read the
 * same zero for "this position is closed" and "this is not a position at all",
 * which silently froze every watchlist row at its REST price.
 *
 * The gate is now on the field being present. These tests pin both readings,
 * because this hook also prices Positions and Holdings and nothing else covered
 * it.
 */
const marketData = new Map<string, { data: { ltp: number }; lastUpdate: number }>()

vi.mock('./useMarketData', () => ({
  useMarketData: () => ({
    data: marketData,
    isConnected: true,
    isAuthenticated: true,
    isPaused: false,
    isFallbackMode: false,
    error: null,
  }),
}))

vi.mock('./useMarketStatus', () => ({
  useMarketStatus: () => ({
    isMarketOpen: () => true,
    isAnyMarketOpen: () => true,
  }),
}))

vi.mock('@/stores/authStore', () => ({
  useAuthStore: () => ({ apiKey: 'k' }),
}))

vi.mock('@/api/trading', () => ({
  tradingApi: {
    getMultiQuotes: vi.fn(async () => ({
      status: 'success',
      results: [{ symbol: 'RELIANCE', exchange: 'NSE', data: { ltp: 1300, prev_close: 1280 } }],
    })),
  },
}))

describe('useLivePrice closed-position gate', () => {
  beforeEach(() => {
    marketData.clear()
    marketData.set('NSE:RELIANCE', { data: { ltp: 1350 }, lastUpdate: Date.now() })
  })

  it('takes live prices for an item that carries no quantity at all', async () => {
    // A watchlist row. Not a position, so it must never be frozen.
    const { result } = renderHook(() =>
      useLivePrice([{ symbol: 'RELIANCE', exchange: 'NSE' }], { enabled: true })
    )

    await waitFor(() => expect(result.current.data[0].ltp).toBe(1350))
  })

  it('keeps the REST values for a position that has been closed', async () => {
    // quantity is present and zero, which is what "closed" means here.
    const { result } = renderHook(() =>
      useLivePrice([{ symbol: 'RELIANCE', exchange: 'NSE', quantity: 0, ltp: 1000 }], {
        enabled: true,
      })
    )

    await waitFor(() => expect(result.current.data).toHaveLength(1))
    expect(result.current.data[0].ltp).toBe(1000)
  })

  it('takes live prices for an open position', async () => {
    const { result } = renderHook(() =>
      useLivePrice([{ symbol: 'RELIANCE', exchange: 'NSE', quantity: 5, ltp: 1000 }], {
        enabled: true,
      })
    )

    await waitFor(() => expect(result.current.data[0].ltp).toBe(1350))
  })
})

/**
 * preserveBackendPnl (issue #2266).
 *
 * The rows are the two open Kotak contracts from the issue's own tradebook:
 * entered, exited and re-entered the same day. Kotak's average_price is the
 * average of every buy (or every sell) that day, so rebuilding P&L as
 * (LTP - average_price) * quantity drops the P&L of the earlier round trips.
 * The page showed +779.35 and -2,778.10 where the positionbook said +3,909.75
 * and -10,400.00, at the same LTPs (75.60 and 221.00).
 */
describe('useLivePrice preserveBackendPnl', () => {
  const LONG = 'NFO:NIFTY27OCT2622900CE'
  const SHORT = 'NFO:NIFTY27OCT2622600CE'

  const long = {
    symbol: 'NIFTY27OCT2622900CE',
    exchange: 'NFO',
    quantity: 65,
    average_price: 63.61,
    ltp: 75.6,
    pnl: 3909.75,
  }
  const short = {
    symbol: 'NIFTY27OCT2622600CE',
    exchange: 'NFO',
    quantity: -65,
    average_price: 178.26,
    ltp: 221,
    pnl: -10400,
  }

  const live = (key: string, ltp: number) =>
    marketData.set(key, { data: { ltp }, lastUpdate: Date.now() })

  beforeEach(() => {
    marketData.clear()
  })

  it('shows the backend P&L when the live price equals the REST price', async () => {
    live(LONG, 75.6)
    live(SHORT, 221)
    const { result } = renderHook(() =>
      useLivePrice([long, short], { enabled: true, preserveBackendPnl: true })
    )

    await waitFor(() => expect(result.current.data).toHaveLength(2))
    expect(result.current.data[0].pnl).toBeCloseTo(3909.75, 2)
    expect(result.current.data[1].pnl).toBeCloseTo(-10400, 2)
  })

  it('moves only the open quantity with the price, for a long and a short', async () => {
    live(LONG, 80) // +4.40 on 65 long
    live(SHORT, 220) // -1.00 on 65 short
    const { result } = renderHook(() =>
      useLivePrice([long, short], { enabled: true, preserveBackendPnl: true })
    )

    await waitFor(() => expect(result.current.data[0].ltp).toBe(80))
    expect(result.current.data[0].pnl).toBeCloseTo(3909.75 + 65 * 4.4, 2)
    expect(result.current.data[1].pnl).toBeCloseTo(-10400 + 65, 2)
  })

  it('reads the backend LTP and P&L when the broker sends them as strings', async () => {
    live(LONG, 80)
    const stringly = { ...long, ltp: '75.60', pnl: '3909.75' } as unknown as typeof long
    const { result } = renderHook(() =>
      useLivePrice([stringly], { enabled: true, preserveBackendPnl: true })
    )

    await waitFor(() => expect(result.current.data[0].ltp).toBe(80))
    expect(result.current.data[0].pnl).toBeCloseTo(3909.75 + 65 * 4.4, 2)
  })

  it('scales the move by lot_size', async () => {
    live(LONG, 80)
    const { result } = renderHook(() =>
      useLivePrice([{ ...long, lot_size: 0.01 }], { enabled: true, preserveBackendPnl: true })
    )

    await waitFor(() => expect(result.current.data[0].ltp).toBe(80))
    expect(result.current.data[0].pnl).toBeCloseTo(3909.75 + 65 * 4.4 * 0.01, 2)
  })

  it.each([
    ['zero', 0],
    ['a placeholder', 'NA'],
    ['absent', undefined],
  ])('falls back to the rebuild when the backend LTP is %s', async (_label, ltp) => {
    // Without the price the backend P&L was computed at there is no move to
    // measure, so the row is rebuilt from the average as before.
    live(LONG, 80)
    const row = { ...long, ltp } as unknown as typeof long
    const { result } = renderHook(() =>
      useLivePrice([row], { enabled: true, preserveBackendPnl: true })
    )

    await waitFor(() => expect(result.current.data[0].ltp).toBe(80))
    expect(result.current.data[0].pnl).toBeCloseTo((80 - 63.61) * 65, 2)
  })

  it('keeps rebuilding from the average when the option is off', async () => {
    // The trading dock shows open and realised P&L as two figures, so its open
    // figure must stay the open quantity's unrealised P&L.
    live(LONG, 75.6)
    const { result } = renderHook(() => useLivePrice([long], { enabled: true }))

    await waitFor(() => expect(result.current.data[0].ltp).toBe(75.6))
    expect(result.current.data[0].pnl).toBeCloseTo(779.35, 2)
  })
})
