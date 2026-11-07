package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"syscall"
	"time"
)

type trade struct {
	Price    float64   `json:"price"`
	Time     time.Time `json:"at"`
	id       int64
	exchange string
}

type tickerState struct {
	bars   map[int64]bar
	last   *trade
	recent []trade
}

type metrics struct {
	Last       *trade  `json:"last,omitempty"`
	Low        *trade  `json:"recent_low,omitempty"`
	Latest     *bar    `json:"latest,omitempty"`
	Previous   *bar    `json:"previous,omitempty"`
	VWAP       float64 `json:"vwap,omitempty"`
	Volume     int64   `json:"volume"`
	Relative   float64 `json:"relative_volume,omitempty"`
	OpenLow    float64 `json:"opening_low,omitempty"`
	OpenHigh   float64 `json:"opening_high,omitempty"`
}

type snapshot struct {
	At       time.Time          `json:"at"`
	Ready    bool               `json:"ready"`
	Backfill bool               `json:"backfill"`
	State    string             `json:"state"`
	Symbols  map[string]metrics `json:"symbols"`
}

func validBar(b bar) bool {
	return !b.Time.IsZero() && b.Open > 0 && b.Close > 0 && b.Low > 0 &&
		b.High >= max(b.Open, b.Close, b.Low) && b.Low <= min(b.Open, b.Close) && b.Volume > 0
}

func regularTrade(p packet) bool {
	// 只采用常规成交条件，跳过均价、延迟、盘外及其他特殊打印。
	for _, condition := range p.Conditions {
		switch condition {
		case "@", " ", "F", "I":
		default:
			return false
		}
	}
	return p.Price > 0
}

func applyPacket(s *tickerState, p packet, open, close, current time.Time) {
	if p.Type == "x" || p.Type == "c" {
		id := p.ID
		if p.Type == "c" {
			id = p.OriginalID
		}
		removed := func(t trade) bool { return t.id == id && t.exchange == p.Exchange }
		if s.last != nil && removed(*s.last) {
			s.last = nil
		}
		kept := s.recent[:0]
		for _, t := range s.recent {
			if !removed(t) {
				kept = append(kept, t)
			}
		}
		s.recent = kept
		return
	}
	if p.Time.Before(open) || !p.Time.Before(close) || p.Time.After(current.Add(2*time.Second)) {
		return
	}
	switch p.Type {
	case "b", "u":
		if validBar(p.bar) && p.Time.Add(time.Minute).Before(current.Add(2*time.Second)) {
			s.bars[p.Time.Unix()/60] = p.bar
		}
	case "t":
		if regularTrade(p) && !p.Time.Before(current.Add(-30*time.Second)) {
			value := trade{p.Price, p.Time, p.ID, p.Exchange}
			s.recent = append(s.recent, value)
			if s.last == nil || !p.Time.Before(s.last.Time) {
				s.last = &value
			}
		}
	}
}

func aggregate(bars map[int64]bar, start time.Time, minutes int) *bar {
	b := bar{Time: start}
	for i := 0; i < minutes; i++ {
		item, ok := bars[start.Unix()/60+int64(i)]
		if !ok {
			return nil
		}
		if i == 0 {
			b.Open, b.High, b.Low = item.Open, item.High, item.Low
		}
		b.High, b.Low = max(b.High, item.High), min(b.Low, item.Low)
		b.Close = item.Close
		b.Volume += item.Volume
	}
	return &b
}

func (s *tickerState) measure(open, current time.Time, interval, opening time.Duration, baseline []map[int64]bar, prior []time.Time) metrics {
	m := metrics{Last: s.last}
	kept := s.recent[:0]
	for _, t := range s.recent {
		if !t.Time.Before(current.Add(-15 * time.Second)) {
			kept = append(kept, t)
			if m.Low == nil || t.Price < m.Low.Price || (t.Price == m.Low.Price && t.Time.After(m.Low.Time)) {
				value := t
				m.Low = &value
			}
		}
	}
	s.recent = kept

	// 等分钟线迟到修订后再生成确认 K 线。实时成交继续以 5 秒频率上报。
	final := current.Add(-35 * time.Second)
	if final.Before(open) {
		return m
	}
	lastMinute := final.Truncate(time.Minute)
	var weighted float64
	var vwapVolume int64
	for key := open.Unix()/60; key < lastMinute.Unix()/60; key++ {
		if b, ok := s.bars[key]; ok {
			m.Volume += b.Volume
			if b.VWAP > 0 {
				weighted += b.VWAP * float64(b.Volume)
				vwapVolume += b.Volume
			}
		}
	}
	if vwapVolume > 0 {
		m.VWAP = weighted / float64(vwapVolume)
	}
	if !final.Before(open.Add(opening)) {
		if b := aggregate(s.bars, open, int(opening/time.Minute)); b != nil {
			m.OpenLow, m.OpenHigh = b.Low, b.High
		}
	}
	finished := int(final.Sub(open) / interval)
	if finished == 0 {
		return m
	}
	offset := time.Duration(finished-1) * interval
	start := open.Add(offset)
	minutes := int(interval / time.Minute)
	m.Latest = aggregate(s.bars, start, minutes)
	if finished >= 2 {
		m.Previous = aggregate(s.bars, start.Add(-interval), minutes)
	}
	if m.Latest != nil {
		var sum int64
		var n int
		for i, historical := range baseline {
			if b := aggregate(historical, prior[i].Add(offset), minutes); b != nil {
				sum += b.Volume
				n++
			}
		}
		if n >= 3 && sum > 0 {
			m.Relative = float64(m.Latest.Volume) * float64(n) / float64(sum)
		}
	}
	return m
}

type fillResult struct {
	generation int
	bars       map[string][]bar
	err        error
}

func run(ctx context.Context, auth credentials, symbols []string, open, close time.Time, interval, opening time.Duration, prior []time.Time) error {
	states := make(map[string]*tickerState)
	for _, symbol := range symbols {
		states[symbol] = &tickerState{bars: make(map[int64]bar)}
	}
	client := &http.Client{Timeout: 12 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	streams := make(chan streamBatch, 64)
	go stream(ctx, auth, symbols, streams)
	baselines := make(chan map[string][]map[int64]bar, 1)
	go func() {
		history := make(map[string][]map[int64]bar)
		for _, start := range prior {
			values, err := minuteBars(ctx, client, auth, symbols, start, start.Add(389*time.Minute))
			if err != nil {
				log.Print(err)
			}
			for _, symbol := range symbols {
				bars := make(map[int64]bar)
				for _, b := range values[symbol] {
					if validBar(b) {
						bars[b.Time.Unix()/60] = b
					}
				}
				history[symbol] = append(history[symbol], bars)
			}
		}
		baselines <- history
	}()
	var baseline map[string][]map[int64]bar
	fills := make(chan fillResult, 1)
	state, connected, complete := "正在连接 Alpaca", false, false
	generation, pending := 0, false
	reconciled := false
	var nextFill time.Time
	requestFill := func() {
		if !connected || reconciled || pending || time.Now().Before(nextFill) {
			return
		}
		pending = true
		version := generation
		go func() {
			end := time.Now().UTC().Truncate(time.Minute).Add(-time.Minute)
			values, err := minuteBars(ctx, client, auth, symbols, open, end)
			select {
			case fills <- fillResult{version, values, err}:
			case <-ctx.Done():
			}
		}()
	}
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	encoder := json.NewEncoder(os.Stdout)
	for {
		select {
		case <-ctx.Done():
			return nil
		case value := <-baselines:
			baseline = value
		case batch := <-streams:
			if batch.state != "" {
				connected = batch.state == "connected"
				complete = false
				reconciled = false
				generation++
				state = batch.state
				if !connected {
					log.Print(state)
					for symbol := range states {
						states[symbol] = &tickerState{bars: make(map[int64]bar)}
					}
				}
				nextFill = time.Time{}
				requestFill()
			}
			current := time.Now().UTC()
			for _, p := range batch.packets {
				if s := states[p.Symbol]; connected && s != nil {
					applyPacket(s, p, open, close, current)
				}
			}
		case result := <-fills:
			pending = false
			if !connected || result.generation != generation {
				requestFill()
				continue
			}
			if result.err != nil {
				log.Print(result.err)
				nextFill = time.Now().Add(30 * time.Second)
				continue
			}
			for symbol, bars := range result.bars {
				if s := states[symbol]; s != nil {
					for _, b := range bars {
						key := b.Time.Unix() / 60
						if _, seen := s.bars[key]; !seen && validBar(b) && !b.Time.Before(open) && b.Time.Before(close) {
							s.bars[key] = b
						}
					}
				}
			}
			// 连线可能恰好错过分钟发布，稍后再补一次跨接缝的分钟线。
			reconciled = complete
			complete = true
			nextFill = time.Now().Add(45 * time.Second)
		case current := <-ticker.C:
			requestFill()
			out := snapshot{current.UTC(), connected, complete, state, make(map[string]metrics)}
			for symbol, s := range states {
				out.Symbols[symbol] = s.measure(open, current, interval, opening, baseline[symbol], prior)
			}
			if err := encoder.Encode(out); err != nil {
				return err
			}
		}
	}
}

func main() {
	openArg := flag.String("open", "", "常规开盘 UTC")
	closeArg := flag.String("close", "", "常规收盘 UTC")
	symbolArg := flag.String("symbols", "", "代码列表")
	historyArg := flag.String("history", "", "前五个常规交易日开盘时间")
	confirm := flag.Int("confirmation", 5, "确认 K 线分钟数")
	opening := flag.Int("opening", 15, "开盘区间分钟数")
	flag.Parse()
	open, openErr := time.Parse(time.RFC3339, *openArg)
	close, closeErr := time.Parse(time.RFC3339, *closeArg)
	symbols := strings.Split(*symbolArg, ",")
	if openErr != nil || closeErr != nil || !close.After(open) || time.Now().Before(open) || !time.Now().Before(close) ||
		len(symbols) > 30 || *confirm < 1 || *confirm > 15 || *opening < 5 || *opening > 30 {
		log.Fatal("盘中交易时间、订阅名单或确认周期无效")
	}
	valid := regexp.MustCompile(`^[A-Z][A-Z0-9.\-]{0,14}$`)
	for _, symbol := range symbols {
		if !valid.MatchString(symbol) {
			log.Fatal("盘中订阅名单含无效代码")
		}
	}
	var prior []time.Time
	if *historyArg != "" {
		for _, value := range strings.Split(*historyArg, ",") {
			start, err := time.Parse(time.RFC3339, value)
			if err != nil || !start.Before(open) || len(prior) >= 5 {
				log.Fatal("历史分钟线时段无效")
			}
			prior = append(prior, start)
		}
	}
	auth := credentials{strings.TrimSpace(os.Getenv("APCA_API_KEY_ID")), strings.TrimSpace(os.Getenv("APCA_API_SECRET_KEY"))}
	if auth.key == "" || auth.secret == "" {
		log.Fatal("Alpaca Key ID 或 Secret 未配置")
	}
	ctx, signalStop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer signalStop()
	ctx, cancel := context.WithDeadline(ctx, close)
	defer cancel()
	// 后台退出会关闭 stdin，即使父进程异常结束也回收行情子进程。
	go func() {
		_, _ = io.Copy(io.Discard, os.Stdin)
		cancel()
	}()
	if err := run(ctx, auth, symbols, open, close, time.Duration(*confirm)*time.Minute, time.Duration(*opening)*time.Minute, prior); err != nil {
		fmt.Fprintln(os.Stderr, "无法发布盘中行情快照")
		os.Exit(1)
	}
}
