package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

type credentials struct {
	key, secret string
}

func (c credentials) headers() http.Header {
	h := make(http.Header)
	h.Set("APCA-API-KEY-ID", c.key)
	h.Set("APCA-API-SECRET-KEY", c.secret)
	return h
}

type bar struct {
	Time   time.Time `json:"t"`
	Open   float64   `json:"o"`
	High   float64   `json:"h"`
	Low    float64   `json:"l"`
	Close  float64   `json:"c"`
	Volume int64     `json:"v"`
	VWAP   float64   `json:"vw"`
}

type packet struct {
	bar
	Type       string   `json:"T"`
	Symbol     string   `json:"S"`
	Code       int      `json:"code"`
	Trades     []string `json:"trades"`
	Bars       []string `json:"bars"`
	Updated    []string `json:"updatedBars"`
	Price      float64  `json:"p"`
	ID         int64    `json:"i"`
	OriginalID int64    `json:"oi"`
	Exchange   string   `json:"x"`
	Conditions []string `json:"c"`
}

func (p *packet) UnmarshalJSON(data []byte) error {
	// 成交条件与 K 线收盘共用字段 c，按消息类型分别解析。
	type wire packet
	var raw struct {
		Type string          `json:"T"`
		C    json.RawMessage `json:"c"`
		// 显式接收小写 s，避免 Go 将成交股数匹配到大写 S（股票代码）。
		TradeSize json.RawMessage `json:"s"`
		*wire
	}
	raw.wire = (*wire)(p)
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	p.Type = raw.Type
	switch p.Type {
	case "t":
		return json.Unmarshal(raw.C, &p.Conditions)
	case "b", "u":
		return json.Unmarshal(raw.C, &p.Close)
	}
	return nil
}

type streamBatch struct {
	packets []packet
	state   string
}

func publish(ctx context.Context, out chan<- streamBatch, batch streamBatch) {
	select {
	case out <- batch:
	case <-ctx.Done():
	}
}

func allSubscribed(got, symbols []string) bool {
	set := make(map[string]bool, len(got))
	for _, symbol := range got {
		set[symbol] = true
	}
	for _, symbol := range symbols {
		if !set[symbol] {
			return false
		}
	}
	return true
}

func streamReadError(stage string, err error) error {
	var closed *websocket.CloseError
	var network net.Error
	var syntax *json.SyntaxError
	var field *json.UnmarshalTypeError
	switch {
	case errors.As(err, &closed):
		return fmt.Errorf("Alpaca %s失败 · WebSocket 关闭码 %d", stage, closed.Code)
	case errors.As(err, &network) && network.Timeout():
		return fmt.Errorf("Alpaca %s超时", stage)
	case errors.Is(err, syscall.ECONNRESET):
		return fmt.Errorf("Alpaca %s失败 · TCP 连接被重置", stage)
	case errors.Is(err, net.ErrClosed):
		return fmt.Errorf("Alpaca %s失败 · 本地连接已关闭", stage)
	case errors.Is(err, websocket.ErrReadLimit):
		return fmt.Errorf("Alpaca %s失败 · WebSocket 消息超过读取上限", stage)
	case errors.As(err, &syntax):
		return fmt.Errorf("Alpaca %s失败 · JSON 语法异常 · 字节 %d", stage, syntax.Offset)
	case errors.As(err, &field):
		return fmt.Errorf("Alpaca %s失败 · JSON 字段类型异常 · 字段 %q · 预期 %s", stage, field.Field, field.Type)
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		return fmt.Errorf("Alpaca %s失败 · 消息提前结束", stage)
	default:
		return fmt.Errorf("Alpaca %s失败 · 读取错误类型 %T", stage, err)
	}
}

func stream(ctx context.Context, auth credentials, symbols []string, out chan<- streamBatch) {
	delay := time.Second
	for ctx.Err() == nil {
		connectedAt := time.Now()
		err := streamOnce(ctx, auth, symbols, out)
		if ctx.Err() != nil {
			return
		}
		if time.Since(connectedAt) > time.Minute {
			delay = time.Second
		}
		// 只记录受控的状态或错误码，避免回显请求或凭据。
		publish(ctx, out, streamBatch{state: err.Error() + "，正在重连"})
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay = min(2*delay, 30*time.Second)
	}
}

func streamOnce(ctx context.Context, auth credentials, symbols []string, out chan<- streamBatch) error {
	dialer := websocket.Dialer{Proxy: http.ProxyFromEnvironment, HandshakeTimeout: 10 * time.Second}
	conn, resp, err := dialer.DialContext(ctx, "wss://stream.data.alpaca.markets/v2/iex", auth.headers())
	if err != nil {
		if resp != nil {
			defer resp.Body.Close()
			return fmt.Errorf("Alpaca WebSocket 握手 HTTP %d", resp.StatusCode)
		}
		return errors.New("Alpaca WebSocket 连接失败")
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	conn.SetReadLimit(2 << 20)
	const confirmationTimeout = 12 * time.Second
	_ = conn.SetReadDeadline(time.Now().Add(confirmationTimeout))
	var accepted bool
	for !accepted {
		var batch []struct {
			Type string `json:"T"`
			Msg  string `json:"msg"`
			Code int    `json:"code"`
		}
		if err := conn.ReadJSON(&batch); err != nil {
			return streamReadError("鉴权确认", err)
		}
		for _, p := range batch {
			if p.Type == "error" {
				return fmt.Errorf("Alpaca 鉴权错误 %d", p.Code)
			}
			accepted = accepted || (p.Type == "success" && p.Msg == "authenticated")
		}
	}
	_ = conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	if conn.WriteJSON(map[string]any{"action": "subscribe", "trades": symbols, "bars": symbols, "updatedBars": symbols}) != nil {
		return errors.New("Alpaca 订阅请求失败")
	}
	// 订阅确认使用独立的等待窗口，鉴权耗时不会挤占它。
	_ = conn.SetReadDeadline(time.Now().Add(confirmationTimeout))
	keepalive, cancel := context.WithCancel(ctx)
	defer cancel()
	pingFailed := make(chan struct{})
	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-keepalive.Done():
				return
			case <-ticker.C:
				if conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(5*time.Second)) != nil {
					close(pingFailed)
					_ = conn.Close()
					return
				}
			}
		}
	}()
	const timeout = 40 * time.Second
	ready := false
	conn.SetPongHandler(func(string) error {
		if ready {
			return conn.SetReadDeadline(time.Now().Add(timeout))
		}
		return nil
	})
	for {
		if ready {
			_ = conn.SetReadDeadline(time.Now().Add(timeout))
		}
		var batch []packet
		if err := conn.ReadJSON(&batch); err != nil {
			select {
			case <-pingFailed:
				return errors.New("Alpaca 心跳发送失败")
			default:
			}
			if !ready {
				return streamReadError("订阅确认", err)
			}
			return streamReadError("行情读取", err)
		}
		for _, p := range batch {
			switch p.Type {
			case "error":
				return fmt.Errorf("Alpaca 订阅或行情错误 %d", p.Code)
			case "subscription":
				if !allSubscribed(p.Trades, symbols) || !allSubscribed(p.Bars, symbols) || !allSubscribed(p.Updated, symbols) {
					return errors.New("Alpaca 返回的订阅名单不完整")
				}
				if !ready {
					ready = true
					publish(ctx, out, streamBatch{state: "connected"})
				}
			}
		}
		if ready {
			publish(ctx, out, streamBatch{packets: batch})
		}
	}
}

func minuteBars(ctx context.Context, client *http.Client, auth credentials, symbols []string, start, end time.Time) (map[string][]bar, error) {
	result := make(map[string][]bar)
	if end.Before(start) {
		return result, nil
	}
	params := url.Values{
		"symbols": {strings.Join(symbols, ",")}, "timeframe": {"1Min"}, "feed": {"iex"},
		"adjustment": {"raw"}, "start": {start.Format(time.RFC3339)}, "end": {end.Format(time.RFC3339)}, "limit": {"10000"},
	}
	for page := 0; page < 5; page++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://data.alpaca.markets/v2/stocks/bars?"+params.Encode(), nil)
		if err != nil {
			return nil, errors.New("无法构造 Alpaca 补线请求")
		}
		req.Header = auth.headers()
		resp, err := client.Do(req)
		if err != nil {
			return nil, errors.New("Alpaca 分钟线补齐失败")
		}
		var data struct {
			Bars map[string][]bar `json:"bars"`
			Next string           `json:"next_page_token"`
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("Alpaca 分钟线 HTTP %d", resp.StatusCode)
		}
		err = json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&data)
		resp.Body.Close()
		if err != nil {
			return nil, errors.New("Alpaca 分钟线响应无效")
		}
		for symbol, bars := range data.Bars {
			result[symbol] = append(result[symbol], bars...)
		}
		if data.Next == "" {
			return result, nil
		}
		params.Set("page_token", data.Next)
	}
	return nil, errors.New("Alpaca 分钟线未补齐全部页")
}
