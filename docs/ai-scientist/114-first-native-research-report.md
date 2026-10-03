# İlk tamamlanan native araştırma akışı — 2026-10-02

## Geçen

Gerçek yerel AOS model görevi → canonical GPU devri → Scientist araştırması
→ bağımsız puanlama/Referee → AOS rapor alma/kaydetme/geri okuma akışı tamamlandı.
Run: `772de3f1-4291-4044-b7df-c6cf2e5a4ee0`, terminal durum `completed`.
Rapor SHA: `9e031c5d02d62f854d99d6b0234cb6852dd098eb8d42c779b9ecd573536b6afa`.

- Sentetik çalışma modları snapshot'ı; 3 baseline / 9 ölçüm, 1 yerel model önerisi / 1 primary ölçüm.
- Aday VUS-PR/VUS-ROC: 1.0 / 1.0. Baseline'a anlamlı iyileşme sağlamadı; Referee **DISCARD** verdi. Gelişim kanıtı olarak sayılmaz.
- Research wall: 352.15s; ürün budget kaydı 573 model tokenı. Bütçe 1 öneri / 600s / 30000 token.
- Scientist model: Qwen/Qwen3.5-9B, FP8 per-tensor; context8192, output2048.
- AOS Decider: Qwen3_5ForCausalLM, BF16, quantization yok; context16384, output512, temperature0, graphs kapalı.
- AOS iş süresi37.687s; uygulama model çağrısı37.353s. Tek örnek; istatistiksel benchmark değildir.
- Scientist binding VRAM tepe örneği12760MiB; AOS global GPU örnekleyici4022MiB. Kesintisiz kesin tepe ölçümü değildir.
- GPU token23(AOS) ve24(Scientist) `done`; aktif/bekleyen istek yok. Her iki modelin özgün PID ve cgroup'u yok, MainPID0. Director işçisi de kapalı.
- Yalnız sahip olunan native AOS/broker/desktop normal kapanışı exit0 ile doğrulandı. Ortak scheduler değişmedi, release komutu gönderilmedi; kullanıcı arayüzü açık kaldı.

AOS oturumu rapor digest zincirini ve fiziksel kapanışı ayrıca bağımsız
doğruladı. Karşı readback SHA:
`3347ffcf24f6d53c55683b66339d642e9771cada83ffab8c429a46e085c0bad5`.

[Makine okunur kanıt](review-evidence/native-aos-research-completed-20261002.json).
Kaynak çifti: Scientist `fcb3b70e08335f115cc6fc4afe5dbc1eeebd0968`,
AOS `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`. Scientist tracked farkı
boş; AOS tracked fark hash'i `27385fc37e117a2a100a3d5deb1842f1c6d9cd6313857f5eecdaa93a241a1b78`.

## Kalan / başarısız

İkinci AOS model çağrısı başlamadı. İlk intent `receipt_recorded` olsa da
admission için bağımsız resolution kaydı yok; güvenlik kapısı korundu.
Yakalanmayan ScientistAdmissionError HTTP yanıtını JSON dışına çıkardı.
İstek tekrar POST edilmedi; araştırma bağımsız tamamlandı. AOS ile mevcut
retained reconciliation adaptörünün native factory'ye bağlanması çalışılıyor.

Kuyrukta adil GPU devri / her iki tarafın tekrarlı ilerlemesi henüz geçmedi.
GPU bekleme/devir ve Lab startup/inference/drain süreleri runtime binding'de
kaydedilmedi; alanlar null bırakıldı. Timeout bütçeleri ölçüm sayılmaz.

## Bu koşuda çalıştırılmayan

Kontrollü iptal/toparlanma, geniş çok önerili araştırma, gerçek public veri
holdout'u, 32k bağlam kabulü ve LoRA/QLoRA eğitim kabulü çalıştırılmadı.
Bu tek sentetik snapshot koşusu endüstriyel benchmark kabulü değildir;
öğrenilmiş adaptör veya tüm projenin tamamlanması olarak sunulmaz.
Lisans ve genel CI işleri ayrı kabul maddeleridir.
