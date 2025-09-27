import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
import joblib


df = pd.read_csv("dataset.csv")

X = df["texto"]
y = df["categoria"]


vectorizer = TfidfVectorizer()
X_vec = vectorizer.fit_transform(X)


model = MultinomialNB()
model.fit(X_vec, y)


joblib.dump(model, "email_model.pkl")
joblib.dump(vectorizer, "vectorizer.pkl")

print("✅ Modelo treinado e salvo!")