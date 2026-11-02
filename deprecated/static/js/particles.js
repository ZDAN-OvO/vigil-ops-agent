/**
 * 粒子动画模块
 * 负责 Canvas 粒子系统和连接线的绘制
 */
function createParticlesModule() {
    let particleCanvas = null;
    let particleCtx = null;
    let particles = [];
    let connectionDistance = 120;

    function resizeCanvas() {
        if (particleCanvas) {
            particleCanvas.width = window.innerWidth;
            particleCanvas.height = window.innerHeight;
        }
    }

    function animateParticles() {
        if (!particleCtx || !particleCanvas) return;

        const ctx = particleCtx;
        const width = particleCanvas.width;
        const height = particleCanvas.height;

        ctx.clearRect(0, 0, width, height);

        // 获取当前主题颜色
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark' ||
            (!document.documentElement.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
        const particleColor = isDark ? '255, 255, 255' : '0, 113, 227';

        // 更新和绘制粒子
        particles.forEach((particle, i) => {
            // 更新位置
            particle.x += particle.vx;
            particle.y += particle.vy;

            // 边界反弹
            if (particle.x < 0 || particle.x > width) particle.vx *= -1;
            if (particle.y < 0 || particle.y > height) particle.vy *= -1;

            // 绘制粒子
            ctx.beginPath();
            ctx.arc(particle.x, particle.y, particle.radius, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(${particleColor}, ${particle.opacity})`;
            ctx.fill();

            // 绘制连接线
            for (let j = i + 1; j < particles.length; j++) {
                const other = particles[j];
                const dx = particle.x - other.x;
                const dy = particle.y - other.y;
                const distance = Math.sqrt(dx * dx + dy * dy);

                if (distance < connectionDistance) {
                    const opacity = (1 - distance / connectionDistance) * 0.15;
                    ctx.beginPath();
                    ctx.moveTo(particle.x, particle.y);
                    ctx.lineTo(other.x, other.y);
                    ctx.strokeStyle = `rgba(${particleColor}, ${opacity})`;
                    ctx.lineWidth = 0.5;
                    ctx.stroke();
                }
            }
        });

        requestAnimationFrame(animateParticles);
    }

    return {
        // 初始化 Canvas 粒子系统
        initParticles() {
            const canvas = document.createElement('canvas');
            canvas.id = 'particleCanvas';
            document.body.insertBefore(canvas, document.body.firstChild);

            particleCanvas = canvas;
            particleCtx = canvas.getContext('2d');
            particles = [];
            connectionDistance = 120;

            resizeCanvas();
            window.addEventListener('resize', resizeCanvas);

            // 创建粒子
            const particleCount = 60;
            for (let i = 0; i < particleCount; i++) {
                particles.push({
                    x: Math.random() * canvas.width,
                    y: Math.random() * canvas.height,
                    vx: (Math.random() - 0.5) * 0.5,
                    vy: (Math.random() - 0.5) * 0.5,
                    radius: Math.random() * 2 + 1,
                    opacity: Math.random() * 0.5 + 0.2
                });
            }

            animateParticles();
        }
    };
}
