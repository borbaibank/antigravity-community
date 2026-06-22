$state = @{}
$openOrders = @()

function Reset-State {
  $script:state = @{
    open_positions = [ordered]@{
      'ETHUSDT_TabA' = @{
        tab='TabA'; symbol='ETHUSDT'; side='Long'; position_side='LONG';
        entry_price=3000.0; sl=2940.0; tp=3120.0; qty=0.5;
        entry_time='2026-04-17T10:00:00'; sl_order_id=101; tp_order_id=102
      }
      'ETHUSDT_TabB' = @{
        tab='TabB'; symbol='ETHUSDT'; side='Short'; position_side='SHORT';
        entry_price=3010.0; sl=3070.0; tp=2890.0; qty=0.5;
        entry_time='2026-04-17T10:05:00'; sl_order_id=201; tp_order_id=202
      }
    }
  }
  $script:openOrders = [System.Collections.ArrayList]@(
    @{ symbol='ETHUSDT'; orderId=101; type='STOP_MARKET'; side='SELL'; positionSide='LONG' },
    @{ symbol='ETHUSDT'; orderId=102; type='TAKE_PROFIT_MARKET'; side='SELL'; positionSide='LONG' },
    @{ symbol='ETHUSDT'; orderId=201; type='STOP_MARKET'; side='BUY'; positionSide='SHORT' },
    @{ symbol='ETHUSDT'; orderId=202; type='TAKE_PROFIT_MARKET'; side='BUY'; positionSide='SHORT' }
  )
}

function Get-PosSide($pos) {
  if ($pos.position_side) { return [string]$pos.position_side }
  if ($pos.side -eq 'Long') { return 'LONG' }
  return 'SHORT'
}

function Cancel-AllAlgoOrders($symbol, $positionSide) {
  $script:openOrders = [System.Collections.ArrayList]@(
    @($script:openOrders | Where-Object {
      -not ($_.symbol -eq $symbol -and $_.positionSide -eq $positionSide -and $_.type -in @('STOP_MARKET','TAKE_PROFIT_MARKET'))
    })
  )
}

function Cancel-AllOrders($symbol, $positionSide) {
  $script:openOrders = [System.Collections.ArrayList]@(
    @($script:openOrders | Where-Object {
      -not ($_.symbol -eq $symbol -and $_.positionSide -eq $positionSide)
    })
  )
}

function Cancel-OrderById($orderId) {
  $script:openOrders = [System.Collections.ArrayList]@(
    @($script:openOrders | Where-Object { $_.orderId -ne $orderId })
  )
}

function Close-PositionUnsafe($posKey, $reason, $skipExchange=$false) {
  if (-not $state.open_positions.Contains($posKey)) { return "MISSING $posKey" }
  $pos = $state.open_positions[$posKey]
  $sym = $pos.symbol
  $posSide = Get-PosSide $pos
  if (-not $skipExchange) {
    Cancel-AllAlgoOrders $sym $posSide
    Cancel-AllOrders $sym $posSide
  }
  $state.open_positions.Remove($posKey)
  return "CLOSED $posKey reason=$reason posSide=$posSide"
}

function Handle-OrderUpdate($event) {
  if ($event.status -ne 'FILLED') { return 'IGNORED non-filled' }
  Cancel-OrderById $event.orderId
  foreach ($entry in @($state.open_positions.GetEnumerator())) {
    $posKey = $entry.Key
    $pos = $entry.Value
    if ($pos.symbol -ne $event.symbol) { continue }
    $posSide = Get-PosSide $pos
    if ($event.posSide -and $event.posSide -ne $posSide) { continue }
    $closeSide = if ($pos.side -eq 'Long') { 'SELL' } else { 'BUY' }
    if ($event.orderSide -ne $closeSide) { continue }
    $isSl = ($event.orderId -eq $pos.sl_order_id) -or (($null -eq $pos.sl_order_id -or $pos.sl_order_id -eq 0) -and $event.orderType -eq 'STOP_MARKET')
    $isTp = ($event.orderId -eq $pos.tp_order_id) -or (($null -eq $pos.tp_order_id -or $pos.tp_order_id -eq 0) -and $event.orderType -in @('TAKE_PROFIT_MARKET','TAKE_PROFIT'))
    if ($isSl) {
      if ($pos.tp_order_id) { Cancel-OrderById $pos.tp_order_id }
      return (Close-PositionUnsafe $posKey 'SL' $true)
    }
    if ($isTp) {
      if ($pos.sl_order_id) { Cancel-OrderById $pos.sl_order_id }
      return (Close-PositionUnsafe $posKey 'TP' $true)
    }
  }
  return 'NO MATCH'
}

function Snapshot($title) {
  "`n=== $title ==="
  'Positions:'
  if ($state.open_positions.Count -eq 0) { '- none' }
  foreach ($entry in $state.open_positions.GetEnumerator()) {
    $p = $entry.Value
    "- $($entry.Key) side=$($p.side) posSide=$(Get-PosSide $p) sl=$($p.sl_order_id) tp=$($p.tp_order_id)"
  }
  'Orders:'
  if ($openOrders.Count -eq 0) { '- none' }
  foreach ($o in $openOrders | Sort-Object orderId) {
    "- oid=$($o.orderId) type=$($o.type) side=$($o.side) posSide=$($o.positionSide)"
  }
}

Reset-State
Snapshot 'Initial state'
Close-PositionUnsafe 'ETHUSDT_TabA' 'Manual' $false
Snapshot 'After manual close LONG TabA'
Handle-OrderUpdate ([pscustomobject]@{ status='FILLED'; orderId=202; symbol='ETHUSDT'; posSide='SHORT'; orderSide='BUY'; orderType='TAKE_PROFIT_MARKET' })
Snapshot 'After SHORT TP fill'

Reset-State
Snapshot 'Reset state'
Close-PositionUnsafe 'ETHUSDT_TabB' 'Manual' $false
Snapshot 'After manual close SHORT TabB'
Handle-OrderUpdate ([pscustomobject]@{ status='FILLED'; orderId=101; symbol='ETHUSDT'; posSide='LONG'; orderSide='SELL'; orderType='STOP_MARKET' })
Snapshot 'After LONG SL fill'
